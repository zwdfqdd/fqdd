from typing import Optional, Tuple, Dict

import torch
import torch.nn as nn

from fqdd.nnets.base_utils import FQDD_ACTIVATIONS
from fqdd.modules.attentions import T_CACHE


class ConvolutionalSpatialGatingUnit(torch.nn.Module):
    """Convolutional Spatial Gating Unit (CSGU)."""

    def __init__(
            self,
            size: int,
            kernel_size: int,
            dropout_rate: float,
            use_linear_after_conv: bool,
            gate_activation: str,
            causal: bool = True,
    ):
        super().__init__()

        # split input channels
        n_channels = size // 2
        self.norm = nn.LayerNorm(n_channels)
        # self.lorder is used to distinguish if it's a causal convolution,
        # if self.lorder > 0: it's a causal convolution, the input will be
        #    padded with self.lorder frames on the left in forward.
        # else: it's a symmetrical convolution
        if causal:
            padding = 0
            self.lorder = kernel_size - 1
        else:
            # kernel_size should be an odd number for none causal convolution
            assert (kernel_size - 1) % 2 == 0
            padding = (kernel_size - 1) // 2
            self.lorder = 0
        self.conv = torch.nn.Conv1d(
            n_channels,
            n_channels,
            kernel_size,
            1,
            padding,
            groups=n_channels,
        )
        if use_linear_after_conv:
            self.linear = torch.nn.Linear(n_channels, n_channels)
        else:
            self.linear = None

        if gate_activation == "identity":
            self.act = torch.nn.Identity()
        else:
            self.act = FQDD_ACTIVATIONS[gate_activation]()

        self.dropout = torch.nn.Dropout(dropout_rate)

    def espnet_initialization_fn(self):
        torch.nn.init.normal_(self.conv.weight, std=1e-6)
        torch.nn.init.ones_(self.conv.bias)
        if self.linear is not None:
            torch.nn.init.normal_(self.linear.weight, std=1e-6)
            torch.nn.init.ones_(self.linear.bias)

    def forward(
            self, x: torch.Tensor, cache: torch.Tensor = torch.zeros((0, 0, 0))
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Forward method

        Args:
            x (torch.Tensor): (batch, time, channels)
            cache (torch.Tensor): left context cache, it is only
                used in causal convolution (#batch, channels, cache_t),
                (0, 0, 0) meas fake cache.

        Returns:
            out (torch.Tensor): (batch, time, channels/2)
        """

        x_r, x_g = x.chunk(2, dim=-1)
        # exchange the temporal dimension and the feature dimension
        x_g = x_g.transpose(1, 2)  # (#batch, channels, time)

        if self.lorder > 0:
            if cache.size(2) == 0:  # cache_t == 0
                x_g = nn.functional.pad(x_g, (self.lorder, 0), 'constant', 0.0)
            else:
                assert cache.size(0) == x_g.size(0)  # equal batch
                assert cache.size(1) == x_g.size(1)  # equal channel
                x_g = torch.cat((cache, x_g), dim=2)
            assert (x_g.size(2) > self.lorder)
            new_cache = x_g[:, :, -self.lorder:]
        else:
            # It's better we just return None if no cache is required,
            # However, for JIT export, here we just fake one tensor instead of
            # None.
            new_cache = torch.zeros((0, 0, 0),
                                    dtype=x_g.dtype,
                                    device=x_g.device)

        x_g = x_g.transpose(1, 2)
        x_g = self.norm(x_g)  # (N, T, D/2)
        x_g = self.conv(x_g.transpose(1, 2)).transpose(1, 2)  # (N, T, D/2)
        if self.linear is not None:
            x_g = self.linear(x_g)

        x_g = self.act(x_g)
        out = x_r * x_g  # (N, T, D/2)
        out = self.dropout(out)
        return out, new_cache


class ConvolutionalGatingMLP(torch.nn.Module):
    """Convolutional Gating MLP (cgMLP)."""

    def __init__(
            self,
            size: int,
            linear_units: int,
            kernel_size: int,
            dropout_rate: float,
            use_linear_after_conv: bool,
            gate_activation: str,
            causal: bool = True,
    ):
        super().__init__()

        self.channel_proj1 = torch.nn.Sequential(
            torch.nn.Linear(size, linear_units), torch.nn.GELU())
        self.csgu = ConvolutionalSpatialGatingUnit(
            size=linear_units,
            kernel_size=kernel_size,
            dropout_rate=dropout_rate,
            use_linear_after_conv=use_linear_after_conv,
            gate_activation=gate_activation,
            causal=causal,
        )
        self.channel_proj2 = torch.nn.Linear(linear_units // 2, size)

    def forward(
            self,
            x: torch.Tensor,
            mask: torch.Tensor,
            cache: torch.Tensor = torch.zeros((0, 0, 0))
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Forward method

        Args:
            x (torch.Tensor): (batch, time, channels)
            mask_pad (torch.Tensor): used for batch padding (#batch, 1, time),
                (0, 0, 0) means fake mask. Not used yet
            cache (torch.Tensor): left context cache, it is only
                used in causal convolution (#batch, channels, cache_t),
                (0, 0, 0) meas fake cache.

        Returns:
            out (torch.Tensor): (batch, time, channels/2)
        """

        xs_pad = x

        # size -> linear_units
        xs_pad = self.channel_proj1(xs_pad)

        # linear_units -> linear_units/2
        xs_pad, new_cnn_cache = self.csgu(xs_pad, cache)

        # linear_units/2 -> size
        xs_pad = self.channel_proj2(xs_pad)

        out = xs_pad

        return out, new_cnn_cache


class EBranchformerEncoderLayer(torch.nn.Module):
    """E-Branchformer encoder layer module.

    Args:
        size (int): model dimension
        attn: standard self-attention or efficient attention
        cgmlp: ConvolutionalGatingMLP
        feed_forward: feed-forward module, optional
        feed_forward: macaron-style feed-forward module, optional
        dropout_rate (float): dropout probability
        merge_conv_kernel (int): kernel size of the depth-wise conv in merge module
    """

    def __init__(
            self,
            size: int,
            attn: torch.nn.Module,
            cgmlp: torch.nn.Module,
            feed_forward: Optional[torch.nn.Module],
            feed_forward_macaron: Optional[torch.nn.Module],
            src_attn: Optional[nn.Module],
            dropout_rate: float,
            merge_conv_kernel: int = 3,
            causal: bool = True,
            stochastic_depth_rate=0.0,
    ):
        super().__init__()

        self.size = size
        self.attn = attn
        self.cgmlp = cgmlp

        self.feed_forward = feed_forward
        self.feed_forward_macaron = feed_forward_macaron
        self.ff_scale = 1.0
        if self.feed_forward is not None:
            self.norm_ff = nn.LayerNorm(size)
        if self.feed_forward_macaron is not None:
            self.ff_scale = 0.5
            self.norm_ff_macaron = nn.LayerNorm(size)
        self.src_attn = src_attn
        self.norm_mha = nn.LayerNorm(size)  # for the MHA module
        self.norm_mlp = nn.LayerNorm(size)  # for the MLP module
        # for the final output of the block
        self.norm_final = nn.LayerNorm(size)

        self.dropout = torch.nn.Dropout(dropout_rate)

        if causal:
            padding = 0
            self.lorder = merge_conv_kernel - 1
        else:
            # kernel_size should be an odd number for none causal convolution
            assert (merge_conv_kernel - 1) % 2 == 0
            padding = (merge_conv_kernel - 1) // 2
            self.lorder = 0
        self.depthwise_conv_fusion = torch.nn.Conv1d(
            size + size,
            size + size,
            kernel_size=merge_conv_kernel,
            stride=1,
            padding=padding,
            groups=size + size,
            bias=True,
        )

        self.merge_proj = torch.nn.Linear(size + size, size)
        self.stochastic_depth_rate = stochastic_depth_rate

    def _forward(
            self,
            x: torch.Tensor,
            mask: torch.Tensor,
            pos_emb: torch.Tensor,
            mask_pad: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool),
            cache: Optional[Dict[str, Optional[T_CACHE]]] = None,
            cnn_cache: torch.Tensor = torch.zeros((0, 0, 0, 0)),
            stoch_layer_coeff: float = 1.0
    ) -> Tuple[torch.Tensor, torch.Tensor, T_CACHE, torch.Tensor]:
        if cache is not None:
            att_cache = cache['self_att_cache']
            cross_att_cache = cache['cross_att_cache']
        else:
            att_cache, cross_att_cache = (torch.zeros(
                (0, 0, 0, 0)), torch.zeros(0, 0, 0, 0)), (torch.zeros(
                (0, 0, 0, 0)), torch.zeros(0, 0, 0, 0))

        if self.feed_forward_macaron is not None:
            residual = x
            x = self.norm_ff_macaron(x)
            x = residual + stoch_layer_coeff * self.ff_scale * self.dropout(
                self.feed_forward_macaron(x))

        # Two branches
        x1 = x
        x2 = x

        # Branch 1: multi-headed attention module
        x1 = self.norm_mha(x1)
        x_att, new_att_cache = self.attn(x1, x1, x1, mask, pos_emb, att_cache)
        x1 = self.dropout(x_att)
        if cache is not None:
            cache['self_att_cache'] = new_att_cache
        # Branch 2: convolutional gating mlp
        # Fake new cnn cache here, and then change it in conv_module
        new_cnn_cache = torch.zeros((0, 0, 0), dtype=x.dtype, device=x.device)
        x2 = self.norm_mlp(x2)
        x2, new_cnn_cache = self.cgmlp(x2, mask_pad, cnn_cache)
        x2 = self.dropout(x2)

        # Merge two branches
        x_concat = torch.cat([x1, x2], dim=-1)
        x_tmp = x_concat.transpose(1, 2)
        if self.lorder > 0:
            x_tmp = nn.functional.pad(x_tmp, (self.lorder, 0), "constant", 0.0)
            assert x_tmp.size(2) > self.lorder
        x_tmp = self.depthwise_conv_fusion(x_tmp)
        x_tmp = x_tmp.transpose(1, 2)

        x = x + stoch_layer_coeff * self.dropout(
            self.merge_proj(x_concat + x_tmp))

        if self.feed_forward is not None:
            # feed forward module
            residual = x
            x = self.norm_ff(x)
            x = residual + stoch_layer_coeff * self.ff_scale * self.dropout(
                self.feed_forward(x))
        residual = x
        if cross_att_cache is None:
            cross_att_cache = (torch.empty(0, 0, 0,
                                           0), torch.empty(0, 0, 0, 0))
        x, new_cross_cache = self.src_attn(x1,
                                           x2,
                                           x2,
                                           mask,
                                           cache=cross_att_cache)
        if cache is not None:
            cache['cross_att_cache'] = new_cross_cache
        x = residual + self.dropout(x)
        x = self.norm_final(x)

        return x, mask, cache, new_cnn_cache

    def forward(
            self,
            x: torch.Tensor,
            mask: torch.Tensor,
            pos_emb: torch.Tensor,
            mask_pad: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool),
            cache: Optional[Dict[str, Optional[T_CACHE]]] = None,
            cnn_cache: torch.Tensor = torch.zeros((0, 0, 0, 0)),
    ) -> Tuple[torch.Tensor, torch.Tensor, T_CACHE, torch.Tensor]:
        """Compute encoded features.

        Args:
            x (Union[Tuple, torch.Tensor]): Input tensor  (#batch, time, size).
            mask (torch.Tensor): Mask tensor for the input (#batch, time, time).
            pos_emb (torch.Tensor): positional encoding, must not be None
                for BranchformerEncoderLayer.
            mask_pad (torch.Tensor): batch padding mask used for conv module.
                (#batch, 1，time), (0, 0, 0) means fake mask.
            att_cache (torch.Tensor): Cache tensor of the KEY & VALUE
                (#batch=1, head, cache_t1, d_k * 2), head * d_k == size.
            cnn_cache (torch.Tensor): Convolution cache in cgmlp layer
                (#batch=1, size, cache_t2)

        Returns:
            torch.Tensor: Output tensor (#batch, time, size).
            torch.Tensor: Mask tensor (#batch, time, time.
            torch.Tensor: att_cache tensor,
                (#batch=1, head, cache_t1 + time, d_k * 2).
            torch.Tensor: cnn_cahce tensor (#batch, size, cache_t2).
        """

        stoch_layer_coeff = 1.0
        # with stochastic depth, residual connection `x + f(x)` becomes
        # `x <- x + 1 / (1 - p) * f(x)` at training time.
        if self.training:
            stoch_layer_coeff = 1.0 / (1 - self.stochastic_depth_rate)
        return self._forward(x, mask, pos_emb, mask_pad, cache, cnn_cache,
                             stoch_layer_coeff)
