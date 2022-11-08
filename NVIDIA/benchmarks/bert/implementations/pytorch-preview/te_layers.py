# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import transformer_engine.pytorch.cpp_extensions as ext
import transformer_engine_extensions as tex
import transformer_engine.pytorch.fp8 as fp8
from transformer_engine.pytorch import fp8_autocast, LayerNorm
from transformer_engine.pytorch.distributed import _set_cuda_rng_state
from transformer_engine.pytorch.module import TransformerEngineBaseModule
from transformer_engine.common import recipe
from typing import Union, Dict, Any, Tuple
import fmhalib as fmha
import fp8_mha_api

_CUBLASLT_WORKSPACE_SIZE_BYTES = 33_554_432  # 32MiB
_2X_ACC_FPROP = False
_2X_ACC_DGRAD = False
_2X_ACC_WGRAD = False

META_QKV  = tex.FP8FwdTensors.GEMM1_OUTPUT
META_O    = tex.FP8FwdTensors.GEMM2_INPUT
META_DO   = tex.FP8BwdTensors.GRAD_INPUT2
META_DQKV = tex.FP8BwdTensors.GRAD_OUTPUT1

# New scales.
META_S    = tex.FP8FwdTensors.GEMM2_OUTPUT
META_DP   = tex.FP8BwdTensors.GRAD_INPUT1 #TODO this is E5M2!

class _MHA(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx,
        inp: torch.Tensor,
        qkv_weight: torch.Tensor,
        qkv_weight_fp8: torch.Tensor,
        qkv_weight_t_fp8: torch.Tensor,
        qkv_bias: torch.Tensor,
        proj_weight: torch.Tensor,
        proj_weight_fp8: torch.Tensor,
        proj_weight_t_fp8: torch.Tensor,
        proj_bias: torch.Tensor,
        cu_seqlens: torch.Tensor,
        num_attention_heads: int,
        p_dropout: float,
        max_s: int,
        set_zero: bool,
        fp8_meta: Dict[str, Any],
        workspace: torch.Tensor,
        is_training: bool,
        ntokens: Any
    ) -> torch.Tensor:
        assert inp.dim() == 2
        # Make sure input dimensions are compatible
        in_features = qkv_weight.shape[-1]
        h = num_attention_heads
        d = in_features // h

        fp8_dtype_forward = fp8.get_fp8_te_dtype(fp8_meta["recipe"], fprop_tensor=True)

        inputmat, inputmat_t = ext.fp8_cast_transpose_fused(
            inp,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM1_INPUT,
            fp8_dtype_forward,
        )
        ext.fp8_cast_transpose_fused(
            qkv_weight,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM1_WEIGHT,
            fp8_dtype_forward,
            cast_out=qkv_weight_fp8,
            transpose_out=qkv_weight_t_fp8,
        )
        qkv_out = torch.empty(
            inputmat.shape[0],
            qkv_weight.shape[0],
            dtype=torch.uint8,
            device="cuda",
        )
        ext.fp8_gemm(
            qkv_weight_fp8,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM1_WEIGHT],
            fp8_dtype_forward,
            inputmat,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM1_INPUT],
            fp8_dtype_forward,
            torch.int8,
            workspace,
            bias=qkv_bias,
            use_bias=True,
            out = qkv_out,
            out_index = META_QKV,
            fp8_meta_tensor = fp8_meta["scaling_fwd"],
            use_split_accumulator=_2X_ACC_FPROP,
        )

        # FMHA
        b = cu_seqlens.numel() - 1
        is_nl = False
        if b < 4 and b > 1:
            max_s = 512
            is_nl = True
        qkv_out = qkv_out.view(-1, 3, h, d)
        rng_state = torch.cuda.get_rng_state()

        context_, M, Z = fp8_mha_api.fwd(
                qkv_out,
                cu_seqlens,
                fp8_meta["scaling_fwd"].scale_inv[META_QKV], #d_scale_qkv
                fp8_meta["scaling_fwd"].scale[META_S], #q_scale_s
                fp8_meta["scaling_fwd"].scale[META_O],   #q_scale_o
                fp8_meta["scaling_fwd"].amax_history[0][META_S],   #amax_s
                fp8_meta["scaling_fwd"].amax_history[0][META_O],   #amax_o
                fp8_meta["scaling_fwd"].scale_inv[META_S],   #d_scale_s
                fp8_meta["scaling_fwd"].scale_inv[META_O],   #d_scale_o
                p_dropout,
                max_s,
                is_training,
                set_zero,
                None, # gen
                ntokens)

        context = context_.view(-1, in_features)
        context_t = tex.fp8_transpose(context, fp8_dtype_forward)

        ext.fp8_cast_transpose_fused(
            proj_weight,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM2_WEIGHT,
            fp8_dtype_forward,
            cast_out=proj_weight_fp8,
            transpose_out=proj_weight_t_fp8,
        )
        proj_out = ext.fp8_gemm(
            proj_weight_fp8,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM2_WEIGHT],
            fp8_dtype_forward,
            context,
            fp8_meta["scaling_fwd"].scale_inv[META_O],
            fp8_dtype_forward,
            torch.float16,
            workspace,
            bias=proj_bias,
            use_bias=True,
            use_split_accumulator=_2X_ACC_FPROP,
        )
        
        ctx.save_for_backward(
            inputmat_t, qkv_weight_t_fp8, workspace,
            qkv_out, #S_dmask,
            M, Z,
            context_, context_t,
            proj_weight_t_fp8,
            fp8_meta["scaling_fwd"].scale.clone(),
            fp8_meta["scaling_fwd"].scale_inv.clone(),
        )
        ctx.fp8_meta = fp8_meta
        ctx.cu_seqlens = cu_seqlens
        ctx.p_dropout = p_dropout
        ctx.max_s = max_s
        ctx.set_zero = set_zero
        ctx.is_nl = is_nl
        ctx.hidden_size = in_features
        ctx.num_attention_heads = num_attention_heads
        ctx.rng_state = rng_state
        ctx.ntokens = ntokens

        return proj_out

    @staticmethod
    def backward(
        ctx, grad_output: torch.Tensor
    ) -> Tuple[Union[torch.Tensor, None], ...]:
        TransformerEngineBaseModule.pre_backward(True, ctx.fp8_meta)
        (
            inputmat_t,
            qkv_weight_t_fp8,
            workspace,
            qkv_out,
            #S_dmask,
            M, Z,
            context, context_t,
            proj_weight_t_fp8,
            fwd_scales,
            fwd_scale_inverses,
        ) = ctx.saved_tensors
        fp8_dtype_forward = fp8.get_fp8_te_dtype(
            ctx.fp8_meta["recipe"], fprop_tensor=True
        )
        fp8_dtype_backward = fp8.get_fp8_te_dtype(
            ctx.fp8_meta["recipe"], fprop_tensor=False
        )
        proj_bgrad, proj_grad_output_c, proj_grad_output_t = ext.fp8_cast_transpose_bgrad_fused(
            grad_output,
            ctx.fp8_meta["scaling_bwd"],
            tex.FP8BwdTensors.GRAD_OUTPUT2,
            fp8_dtype_backward,
        )
        # PROJ DGRAD
#        proj_dgrad = torch.empty(
#            grad_output.shape[0],
#            ctx.hidden_size,
#            dtype=torch.int8,
#            device="cuda",
#        )
#        print ('PROJ_DGRAD')
        proj_dgrad = ext.fp8_gemm(
            proj_weight_t_fp8,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM2_WEIGHT],
            fp8_dtype_forward,
            proj_grad_output_c,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT2],
            fp8_dtype_backward,
            torch.float16,
            workspace,
#            bias=proj_bgrad,
#            use_bias=False,
#            out=proj_dgrad,
#            out_index=META_DO,
            use_split_accumulator=_2X_ACC_DGRAD,
        )
        # PROJ WGRAD
        proj_wgrad = ext.fp8_gemm(
            context_t,
            fwd_scale_inverses[META_O],
            fp8_dtype_forward,
            proj_grad_output_t,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT2],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_WGRAD,
        )
        proj_dgrad = ext.cast_to_fp8(
            proj_dgrad,
            ctx.fp8_meta["scaling_bwd"],
            META_DO,
            fp8_dtype_backward)
        rng_state_old = torch.cuda.get_rng_state() #TODO clone needed??
        _set_cuda_rng_state(ctx.rng_state)

        dqkv, = fp8_mha_api.bwd(proj_dgrad.view_as(context),
                qkv_out,
                context,
                M, Z,
                ctx.cu_seqlens,
                fwd_scale_inverses[META_QKV], # d_scale_qkv,
                fwd_scale_inverses[META_S], # d_scale_s,
                fwd_scale_inverses[META_O], # d_scale_o,
                ctx.fp8_meta['scaling_bwd'].scale_inv[META_DO], # d_scale_do
                fwd_scales[META_S], # q_scale_s
                ctx.fp8_meta['scaling_bwd'].scale[META_DP], # q_scale_dp
                ctx.fp8_meta['scaling_bwd'].scale[META_DQKV], # q_scale_dqkv
                ctx.fp8_meta['scaling_bwd'].amax_history[0][META_DP], # amax_dp
                ctx.fp8_meta['scaling_bwd'].amax_history[0][META_DQKV], # amax_dqkv
                #TODO actually updating d_scale_dp may not be necessary as the kernel produces and consumes dP.
                ctx.fp8_meta['scaling_bwd'].scale_inv[META_DP], # d_scale_dp
                ctx.fp8_meta['scaling_bwd'].scale_inv[META_DQKV], # d_scale_dqkv
                ctx.p_dropout,
                ctx.max_s,
                ctx.set_zero,
                True, #all_e5m2
                None,
                ctx.ntokens
                )

        _set_cuda_rng_state(rng_state_old)
        dqkv = dqkv.view(-1, 3*ctx.hidden_size)
        dqkv = ext.cast_from_fp8(
            dqkv,
            ctx.fp8_meta["scaling_bwd"],
            META_DQKV,
            fp8_dtype_backward,
            ext.TE_DType[torch.float16]
        )
        ####################################################################################
        qkv_bgrad, dqkv_grad_output_c, dqkv_grad_output_t = ext.fp8_cast_transpose_bgrad_fused(
            dqkv,
            ctx.fp8_meta["scaling_bwd"],
            META_DQKV,
            fp8_dtype_backward,
        )
        # QKV DGRAD
        qkv_dgrad = ext.fp8_gemm(
            qkv_weight_t_fp8,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM1_WEIGHT],
            fp8_dtype_forward,
            dqkv_grad_output_c,
            ctx.fp8_meta["scaling_bwd"].scale_inv[META_DQKV],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_DGRAD,
        )
        # QKV WGRAD
        qkv_wgrad = ext.fp8_gemm(
            inputmat_t,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM1_INPUT],
            fp8_dtype_forward,
            dqkv_grad_output_t,
            ctx.fp8_meta["scaling_bwd"].scale_inv[META_DQKV],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_WGRAD,
        )
        TransformerEngineBaseModule.post_backward(
            True, ctx.fp8_meta
        )
        return (qkv_dgrad, 
            qkv_wgrad,
            None,
            None,
            qkv_bgrad,
            proj_wgrad,
            None,
            None,
            proj_bgrad,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None)

class FP8_MHA(TransformerEngineBaseModule):
    def __init__(
        self,
        config,
        params_dtype: torch.dtype = torch.float32):
        super().__init__()
        self.p_dropout = config.attention_probs_dropout_prob
        self.h = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.d = self.hidden_size // self.h
        self.set_zero = True
        assert self.d * self.h == self.hidden_size, "Invalid hidden size/num_heads"

        self.qkv_weight = Parameter(
            torch.empty(
                self.hidden_size * 3,
                self.hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.fp8_weight_shapes.append(self.qkv_weight.shape)
        self.qkv_bias = Parameter(
            torch.empty(
                self.hidden_size * 3,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.proj_weight = Parameter(
            torch.empty(
                self.hidden_size,
                self.hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.fp8_weight_shapes.append(self.proj_weight.shape)
        self.proj_bias = Parameter(
            torch.empty(
                self.hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        with torch.no_grad():
            self.qkv_bias.zero_()
            self.qkv_weight.fill_(1.0)
            self.proj_bias.zero_()
            self.proj_weight.fill_(1.0)
        # workspace for cublasLt
        self.workspace = torch.empty(
            _CUBLASLT_WORKSPACE_SIZE_BYTES, dtype=torch.int8, device="cuda"
        )

    def forward(
        self, inp: torch.Tensor,
        cu_seqlens, max_s, ntokens=None
    ) -> torch.Tensor:
        self.pre_forward(inp, num_gemms=2)

        out = _MHA.apply(
            inp,
            self.qkv_weight,
            self.weight1_fp8,
            self.weight1_t_fp8,
            self.qkv_bias,
            self.proj_weight,
            self.weight2_fp8,
            self.weight2_t_fp8,
            self.proj_bias,
            cu_seqlens,
            self.h,
            self.p_dropout,
            max_s,
            self.set_zero,
            self.fp8_meta,
            self.workspace,
            self.training,
            ntokens)

        self.post_forward()

        return out
        
class _LayerNormMLP(torch.autograd.Function):
    """LayerNormMLP semi-top level module
    Calls custom cuda extensions.
    """

    @staticmethod
    def forward(
        ctx,
        inp: torch.Tensor,
        ln_weight: torch.Tensor,
        ln_bias: torch.Tensor,
        fc1_weight: torch.Tensor,
        fc1_weight_fp8: torch.Tensor,
        fc1_weight_t_fp8: torch.Tensor,
        fc1_bias: torch.Tensor,
        fc2_weight: torch.Tensor,
        fc2_weight_fp8: torch.Tensor,
        fc2_weight_t_fp8: torch.Tensor,
        fc2_bias: torch.Tensor,
        eps: float,
        fp8_meta: Dict[str, Any],
        workspace: torch.Tensor,
        activation_dtype: torch.dtype,
    ) -> Union[Tuple[torch.Tensor, ...], torch.Tensor]:

        fp8_dtype_forward = fp8.get_fp8_te_dtype(fp8_meta["recipe"], fprop_tensor=True)
        ln_out_return, mu, rsigma = tex.layernorm_fwd(
            inp, ln_weight, ln_bias, eps
        )
        ln_out_total = ln_out_return
        ln_out_total, ln_out_total_t = ext.fp8_cast_transpose_fused(
            ln_out_total,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM1_INPUT,
            fp8_dtype_forward,
        )

        ext.fp8_cast_transpose_fused(
            fc1_weight,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM1_WEIGHT,
            fp8_dtype_forward,
            cast_out=fc1_weight_fp8,
            transpose_out=fc1_weight_t_fp8,
        )

        fc1_out = ext.fp8_gemm(
            fc1_weight_fp8,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM1_WEIGHT],
            fp8_dtype_forward,
            ln_out_total,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM1_INPUT],
            fp8_dtype_forward,
            torch.float16,
            workspace,
            bias=fc1_bias,
            use_bias=True,
            use_split_accumulator=_2X_ACC_FPROP,
        )

        gelu_out = ext.fp8_gelu(
            fc1_out,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM2_INPUT,
            fp8_dtype_forward,
        )

        ext.fp8_cast_transpose_fused(
            fc2_weight,
            fp8_meta["scaling_fwd"],
            tex.FP8FwdTensors.GEMM2_WEIGHT,
            fp8_dtype_forward,
            cast_out=fc2_weight_fp8,
            transpose_out=fc2_weight_t_fp8,
        )

        fc2_out = ext.fp8_gemm(
            fc2_weight_fp8,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM2_WEIGHT],
            fp8_dtype_forward,
            gelu_out,
            fp8_meta["scaling_fwd"].scale_inv[tex.FP8FwdTensors.GEMM2_INPUT],
            fp8_dtype_forward,
            torch.float16,
            workspace,
            bias=fc2_bias,
            use_bias=True,
            use_split_accumulator=_2X_ACC_FPROP,
        )

        ctx.save_for_backward(
            inp,
            ln_weight,
            mu,
            rsigma,
            ln_out_total_t,
            fc1_out,
            gelu_out,
            fc1_weight_t_fp8,
            fc2_weight_t_fp8,
            fc1_bias,
            fc2_bias,
            workspace,
            fp8_meta["scaling_fwd"].scale_inv.clone(),
        )
        ctx.activation_dtype = activation_dtype
        ctx.fp8_meta = fp8_meta
        ctx.inp_shape = inp.shape

        fc2_out = fc2_out.view(-1, *inp.shape[1:-1], fc2_out.shape[-1])

        return fc2_out, ln_out_return.view_as(inp)

    @staticmethod
    def backward(
        ctx, *grad_outputs: Tuple[torch.Tensor, ...]
    ) -> Tuple[Union[torch.Tensor, None], ...]:
        TransformerEngineBaseModule.pre_backward(True, ctx.fp8_meta)

        (
            inputmat,
            ln_weight,
            mu,
            rsigma,
            ln_out_total_t,
            fc1_out,
            gelu_out,
            fc1_weight_t_fp8,
            fc2_weight_t_fp8,
            fc1_bias,
            fc2_bias,
            workspace,
            fwd_scale_inverses,
        ) = ctx.saved_tensors

        grad_output = grad_outputs[0].contiguous()
        grad_output_mat = grad_output.view((-1, grad_output.shape[-1]))

        fp8_dtype_forward = fp8.get_fp8_te_dtype(
            ctx.fp8_meta["recipe"], fprop_tensor=True
        )
        fp8_dtype_backward = fp8.get_fp8_te_dtype(
            ctx.fp8_meta["recipe"], fprop_tensor=False
        )

        fc2_bias_grad, grad_output_c, grad_output_t = ext.fp8_cast_transpose_bgrad_fused(
            grad_output_mat,
            ctx.fp8_meta["scaling_bwd"],
            tex.FP8BwdTensors.GRAD_OUTPUT1,
            fp8_dtype_backward,
        )

        # FC2 DGRAD 
        fc2_dgrad = ext.fp8_gemm(
            fc2_weight_t_fp8,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM2_WEIGHT],
            fp8_dtype_forward,
            grad_output_c,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT1],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_DGRAD,
        )

        # FC2 WGRAD
        gelu_out_t = tex.fp8_transpose(gelu_out, fp8_dtype_forward)
        fc2_wgrad = ext.fp8_gemm(
            gelu_out_t,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM2_INPUT],
            fp8_dtype_forward,
            grad_output_t,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT1],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_WGRAD,
        )

        fc1_bias_grad, dgelu, dgelu_t = ext.fp8_cast_transpose_bgrad_dgelu_fused(
            fc2_dgrad,
            fc1_out,
            ctx.fp8_meta["scaling_bwd"],
            tex.FP8BwdTensors.GRAD_OUTPUT2,
            fp8_dtype_backward,
        )

        # FC1 DGRAD
        fc1_dgrad = ext.fp8_gemm(
            fc1_weight_t_fp8,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM1_WEIGHT],
            fp8_dtype_forward,
            dgelu,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT2],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_DGRAD,
        )

        # FC1 WGRAD
        #print ('ln_out_total_t {} dgelu_t {}'.format(ln_out_total_t.shape, dgelu_t.shape))
        fc1_wgrad = ext.fp8_gemm(
            ln_out_total_t,
            fwd_scale_inverses[tex.FP8FwdTensors.GEMM1_INPUT],
            fp8_dtype_forward,
            dgelu_t,
            ctx.fp8_meta["scaling_bwd"].scale_inv[tex.FP8BwdTensors.GRAD_OUTPUT2],
            fp8_dtype_backward,
            torch.float16,
            workspace,
            use_split_accumulator=_2X_ACC_WGRAD,
        )

        # LayerNorm gradient
        d_ln_out = fc1_dgrad.view(-1,inputmat.shape[-1])

        d_ln_out = d_ln_out + grad_outputs[1].view_as(d_ln_out)

        dxmat, dgamma, dbeta = tex.layernorm_bwd(
            d_ln_out, inputmat, mu, rsigma, ln_weight
        )

        TransformerEngineBaseModule.post_backward(
            True, ctx.fp8_meta
        )

        return (
            dxmat.view(ctx.inp_shape),
            dgamma,
            dbeta,
            fc1_wgrad,
            None,
            None,
            fc1_bias_grad,
            fc2_wgrad,
            None,
            None,
            fc2_bias_grad,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )


class LayerNormMLP(TransformerEngineBaseModule):
    """
    Applies layer normalization on the input followed by the MLP module, consisting of
    2 successive linear transformations, separated by the GeLU activation.

    Parameters
    ----------
    hidden_size : int
                 size of each input sample.
    ffn_hidden_size : int
                     intermediate size to which input samples are projected.
    eps : float, default = 1e-5
         a value added to the denominator of layer normalization for numerical stability.

    Optimization parameters
    -----------------------
    params_dtype : torch.dtype, default = `torch.float32`
                  it controls the type used to allocate the initial parameters. Useful when
                  the model is trained with lower precision and the original FP32 parameters
                  would not fit in GPU memory.
    """

    def __init__(
        self,
        hidden_size: int,
        ffn_hidden_size: int,
        eps: float = 1e-5,
        params_dtype: torch.dtype = torch.float32,
    ) -> None:
        super().__init__()

        # LN init
        self.eps = eps
        self.hidden_size = hidden_size
        self.layer_norm_weight = Parameter(
            torch.empty(
                hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.layer_norm_bias = Parameter(
            torch.empty(
                hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        # FC1 init
        self.fc1_weight = Parameter(
            torch.empty(
                ffn_hidden_size,
                hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.fp8_weight_shapes.append(self.fc1_weight.shape)

        self.fc1_bias = Parameter(
            torch.empty(
                ffn_hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )

        # FC2 init
        self.fc2_weight = Parameter(
            torch.empty(
                hidden_size,
                ffn_hidden_size,
                device=torch.cuda.current_device(),
                dtype=params_dtype,
            )
        )
        self.fp8_weight_shapes.append(self.fc2_weight.shape)

        self.fc2_bias = Parameter(
            torch.empty(
                hidden_size, device=torch.cuda.current_device(), dtype=params_dtype
            )
        )

        with torch.no_grad():
            self.layer_norm_bias.zero_()
            self.layer_norm_weight.fill_(1.0)
            self.fc1_bias.zero_()
            self.fc1_weight.fill_(1.0)
            self.fc2_bias.zero_()
            self.fc2_weight.fill_(1.0)

        # workspace for cublasLt
        self.workspace = torch.empty(
            _CUBLASLT_WORKSPACE_SIZE_BYTES, dtype=torch.int8, device="cuda"
        )

    def forward(
        self, inp: torch.Tensor
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, ...]]:
        """
        Apply layer normalization to the input followed by a feedforward network (MLP Block).

        Parameters
        ----------
        inp : torch.Tensor
             Input tensor.
        """

        self.pre_forward(inp, num_gemms=2)

        out = _LayerNormMLP.apply(
            inp,
            self.layer_norm_weight,
            self.layer_norm_bias,
            self.fc1_weight,
            self.weight1_fp8,
            self.weight1_t_fp8,
            self.fc1_bias,
            self.fc2_weight,
            self.weight2_fp8,
            self.weight2_t_fp8,
            self.fc2_bias,
            self.eps,
            self.fp8_meta,
            self.workspace,
            self.activation_dtype,
        )

        self.post_forward()

        out, ln_out = out
        return out, ln_out
