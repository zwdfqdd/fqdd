import math
import torch
import torch.nn as nn

from typing import Optional, Tuple

T_CACHE = Tuple[torch.Tensor, torch.Tensor]


class MultiHeadedAttention(nn.Module):
    """Multi-Head Attention layer.
    if n_kv_head != None and n_kv_head != n_head
    see: https://arxiv.org/pdf/1911.02150.pdf
         https://arxiv.org/pdf/2305.13245.pdf

    Example:
        case 1: n_kv_head == None, head_dim == None, MultiHead attention (MHSA)
        case 2: n_kv_head=1, n_head = 16, MultiQuery attention (MQA)
        case 3: nv_kv_head=2, n_head = 16, GroupedQuery attention (GQA)

    Args:
        n_head (int): The number of heads.
        n_feat (int): The number of features.
        dropout_rate (float): Dropout rate.

    """

    def __init__(self,
                 n_head: int,
                 n_feat: int,
                 dropout_rate: float,
                 query_bias: bool = True,
                 key_bias: bool = True,
                 value_bias: bool = True,
                 n_kv_head: Optional[int] = None,
                 head_dim: Optional[int] = None):
        """Construct an MultiHeadedAttention object."""
        super().__init__()

        self.inner_dim = n_feat if head_dim is None else head_dim * n_head
        if n_kv_head is not None:
            assert head_dim is not None
            self.inner_kv_dim = head_dim * n_kv_head
            n_kv_head = n_kv_head
        else:
            self.inner_kv_dim = self.inner_dim
            n_kv_head = n_head
        # We assume d_v always equals d_k
        self.d_k = self.inner_dim // n_head
        assert self.d_k == self.inner_kv_dim // n_kv_head
        self.h = n_head
        self.h_kv = n_kv_head

        self.linear_q = nn.Linear(n_feat, self.inner_dim, bias=query_bias)
        self.linear_k = nn.Linear(n_feat, self.inner_kv_dim, bias=key_bias)
        self.linear_v = nn.Linear(n_feat, self.inner_kv_dim, bias=value_bias)
        self.linear_out = nn.Linear(self.inner_dim, n_feat, bias=query_bias)
        self.dropout = nn.Dropout(p=dropout_rate)

        self.dropout_rate = dropout_rate

    def _forward_linearx(self, name: str, x: torch.Tensor) -> torch.Tensor:
        assert x.ndim >= 3
        if name == 'query':
            x = self.linear_q(x)
            x_shape = x.size()
            x_shape = x_shape[:-1] + torch.Size([self.h, self.d_k])
        elif name == 'key':
            x = self.linear_k(x)
            x_shape = x.size()
            x_shape = x_shape[:-1] + torch.Size([self.h_kv, self.d_k])
        else:
            assert name == 'value'
            x = self.linear_v(x)
            x_shape = x.size()
            x_shape = x_shape[:-1] + torch.Size([self.h_kv, self.d_k])

        # split last dim
        x = x.view(x_shape)
        x = x.transpose(-3, -2)  # (batch, ...,  head or head_kv, time, d_k)
        return x

    def forward_qkv(
            self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Transform query, key and value.

        Args:
            query (torch.Tensor): Query tensor (#batch, ..., time1, size).
            key (torch.Tensor): Key tensor (#batch, ..., time2, size).
            value (torch.Tensor): Value tensor (#batch, ..., time2, size).

        Returns:
            torch.Tensor: Transformed query tensor, size
                (#batch, ..., n_head, time1, d_k).
            torch.Tensor: Transformed key tensor, size
                (#batch, ..., n_head_kv, time2, d_k).
            torch.Tensor: Transformed value tensor, size
                (#batch, ..., n_head_kv, time2, d_k).

        """
        q = self._forward_linearx('query', query)
        k = self._forward_linearx('key', key)
        v = self._forward_linearx('value', value)
        return q, k, v

    def forward_attention(
            self,
            value: torch.Tensor,
            scores: torch.Tensor,
            mask: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool)
    ) -> torch.Tensor:
        """Compute attention context vector.

        Args:
            value (torch.Tensor): Transformed value, size
                (#batch, ..., n_head, time2, d_k).
            scores (torch.Tensor): Attention score, size
                (#batch, ..., n_head, time1, time2).
            mask (torch.Tensor): Mask, size (#batch, 1, time2) or
                (#batch, ..., time1, time2), (0, ..., 0, 0) means fake mask.

        Returns:
            torch.Tensor: Transformed value (#batch, time1, d_model)
                weighted by the attention score (#batch, time1, time2).

        """
        # NOTE(xcsong): When will `if mask.size(2) > 0` be True?
        #   1. onnx(16/4) [WHY? Because we feed real cache & real mask for the
        #           1st chunk to ease the onnx export.]
        #   2. pytorch training
        if mask.size(-1) > 0:  # time2 > 0
            mask = mask.unsqueeze(-3).eq(0)  # (batch, .., 1, *, time2)
            # For last chunk, time2 might be larger than scores.size(-1)
            mask = mask[..., :scores.size(-1)]  # (batch, 1, *, time2)
            scores = scores.masked_fill(mask, -float('inf'))
            attn = torch.softmax(scores.float(),
                                 dim=-1).type_as(value).masked_fill(
                mask, 0.0)  # (batch, head, time1, time2)
        # NOTE(xcsong): When will `if mask.size(2) > 0` be False?
        #   1. onnx(16/-1, -1/-1, 16/0)
        #   2. jit (16/-1, -1/-1, 16/0, 16/4)
        else:
            attn = torch.softmax(scores.float(), dim=-1).type_as(
                value)  # (batch, ..., head, time1, time2)

        p_attn = self.dropout(attn)
        x = torch.matmul(p_attn, value)  # (batch, ...,  head, time1, d_k)
        x = x.transpose(-3, -2).contiguous()  # [batch, ..., time1, head, d_k]
        x_shape = x.size()[:-2] + torch.Size([self.h * self.d_k])
        x = x.view(x_shape)  # (batch, ..., time1, d_model)
        return self.linear_out(x)  # (batch, ...,  time1, d_model)

    def _update_kv_and_cache(
            self, k: torch.Tensor, v: torch.Tensor,
            cache: T_CACHE) -> Tuple[torch.Tensor, torch.Tensor, T_CACHE]:
        new_cache = cache
        if not self.training:
            # NOTE(xcsong):
            #   when export onnx model, for 1st chunk, we feed
            #       cache(1, head, 0, d_k * 2) (16/-1, -1/-1, 16/0 mode)
            #       or cache(1, head, real_cache_t, d_k * 2) (16/4 mode).
            #       In all modes, `if cache.size(0) > 0` will alwayse be `True`
            #       and we will always do splitting and
            #       concatnation(this will simplify onnx export). Note that
            #       it's OK to concat & split zero-shaped tensors(see code below).
            #   when export jit  model, for 1st chunk, we always feed
            #       cache(0, 0, 0, 0) since jit supports dynamic if-branch.
            # >>> a = torch.ones((1, 2, 0, 4))
            # >>> b = torch.ones((1, 2, 3, 4))
            # >>> c = torch.cat((a, b), dim=2)
            # >>> torch.equal(b, c)        # True
            # >>> d = torch.split(a, 2, dim=-1)
            # >>> torch.equal(d[0], d[1])  # True
            key_cache, value_cache = cache
            if key_cache.size(0) > 0:
                k = torch.cat([key_cache, k], dim=2)
            if value_cache.size(0) > 0:
                v = torch.cat([value_cache, v], dim=2)
            # NOTE(xcsong): We do cache slicing in encoder.forward_chunk, since it's
            #   non-trivial to calculate `next_cache_start` here.
            # new_cache = torch.cat((k, v), dim=-1) if not self.training else cache
            new_cache = (k, v)
        # for multi query or multi group attention
        if self.h_kv != self.h and self.h_kv != 1:
            # NOTE: onnxruntime issues:
            #     https://github.com/wenet-e2e/wenet/issues/2517
            # k = torch.repeat_interleave(
            #     k,
            #     self.h // self.h_kv,
            #     dim=-3,
            # )
            # v = torch.repeat_interleave(
            #     v,
            #     self.h // self.h_kv,
            #     dim=-3,
            # )
            n_repeat = self.h // self.h_kv
            k_shape = k.size()
            k = k.unsqueeze(-3).expand(
                k_shape[:-2] + torch.Size([n_repeat]) +
                k_shape[-2:]).reshape(k_shape[:-3] +
                                      torch.Size([self.h_kv * n_repeat]) +
                                      k_shape[-2:])
            v_shape = v.size()
            v = v.unsqueeze(-3).expand(
                v_shape[:-2] + torch.Size([n_repeat]) +
                v_shape[-2:]).reshape(v_shape[:-3] +
                                      torch.Size([self.h_kv * n_repeat]) +
                                      v_shape[-2:])
        return k, v, new_cache

    def forward(
            self,
            query: torch.Tensor,
            key: torch.Tensor,
            value: torch.Tensor,
            mask: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool),
            pos_emb: torch.Tensor = torch.empty(0),
            cache: T_CACHE = (torch.zeros(0, 0, 0, 0), torch.zeros(0, 0, 0, 0)),
    ) -> Tuple[torch.Tensor, T_CACHE]:
        """Compute scaled dot product attention.

        Args:
            query (torch.Tensor): Query tensor (#batch, time1, size).
            key (torch.Tensor): Key tensor (#batch, time2, size).
            value (torch.Tensor): Value tensor (#batch, time2, size).
            mask (torch.Tensor): Mask tensor (#batch, 1, time2) or
                (#batch, time1, time2).
                1.When applying cross attention between decoder and encoder,
                the batch padding mask for input is in (#batch, 1, T) shape.
                2.When applying self attention of encoder,
                the mask is in (#batch, T, T)  shape.
                3.When applying self attention of decoder,
                the mask is in (#batch, L, L)  shape.
                4.If the different position in decoder see different block
                of the encoder, such as Mocha, the passed in mask could be
                in (#batch, L, T) shape. But there is no such case in current
                Wenet.
            cache (torch.Tensor): Cache tensor (1, head, cache_t, d_k * 2),
                where `cache_t == chunk_size * num_decoding_left_chunks`
                and `head * d_k == size`


        Returns:
            torch.Tensor: Output tensor (#batch, time1, d_model).
            torch.Tensor: Cache tensor (1, head, cache_t + time1, d_k * 2)
                where `cache_t == chunk_size * num_decoding_left_chunks`
                and `head * d_k == size`

        """
        q, k, v = self.forward_qkv(query, key, value)
        k, v, new_cache = self._update_kv_and_cache(k, v, cache)

        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)
        return self.forward_attention(v, scores, mask), new_cache


class MultiHeadedCrossAttention(MultiHeadedAttention):

    def __init__(self,
                 n_head: int,
                 n_feat: int,
                 dropout_rate: float,
                 query_bias: bool = True,
                 key_bias: bool = True,
                 value_bias: bool = True,
                 n_kv_head: Optional[int] = None,
                 head_dim: Optional[int] = None):
        super().__init__(n_head, n_feat, dropout_rate, query_bias, key_bias,
                         value_bias, n_kv_head, head_dim)

    def forward(
            self,
            query: torch.Tensor,
            key: torch.Tensor,
            value: torch.Tensor,
            mask: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool),
            pos_emb: torch.Tensor = torch.empty(0),
            cache: T_CACHE = (torch.zeros((0, 0, 0, 0)), torch.zeros((0, 0, 0, 0)))
    ) -> Tuple[torch.Tensor, T_CACHE]:
        del pos_emb
        key_cache, value_cache = cache
        assert key_cache.size(0) == value_cache.size(0)
        if key_cache.size(0) > 0:
            assert not self.training
            q = self._forward_linearx('query', query)
            k, v = key_cache, value_cache

        else:
            q, k, v = self.forward_qkv(query, key, value)
        new_cache = (k, v) if not self.training else cache
        # for multi query or multi groups attention
        if self.h_kv != self.h and self.h_kv != 1:
            k = torch.repeat_interleave(
                k,
                self.h // self.h_kv,
                dim=-3,
            )
            v = torch.repeat_interleave(
                v,
                self.h // self.h_kv,
                dim=-3,
            )
        B = query.size(0)
        Beams = 1
        if B != k.size(0):
            assert not self.training
            Beams = B // k.size(0)
            B = k.size(0)
            q = q.view(B, Beams, q.size(-3), q.size(-2), q.size(-1))
            k = k.unsqueeze(1)
            v = v.unsqueeze(1)
            mask = mask.unsqueeze(1)

        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)
        output = self.forward_attention(v, scores, mask)

        if query.size(0) != B:
            assert not self.training
            output_shape = torch.Size([B * Beams]) + output.size()[2:]
            output = output.view(output_shape)
        return output, new_cache


class RelPositionMultiHeadedAttention(MultiHeadedAttention):
    """Multi-Head Attention layer with relative position encoding.
    Paper: https://arxiv.org/abs/1901.02860
    Args:
        n_head (int): The number of heads.
        n_feat (int): The number of features.
        dropout_rate (float): Dropout rate.
    """

def __init__(self,
             n_head: int,
             n_feat: int,
             dropout_rate: float,
             query_bias: bool = True,
             key_bias: bool = True,
             value_bias: bool = True,
             n_kv_head: Optional[int] = None,
             head_dim: Optional[int] = None,
             do_rel_shift=False,
             adaptive_scale=False,
             init_weights=False
             ):
    """Construct an RelPositionMultiHeadedAttention object."""
    super().__init__(n_head, n_feat, dropout_rate, query_bias, key_bias,
                     value_bias, n_kv_head, head_dim)
    # linear transformation for positional encoding
    self.linear_pos = nn.Linear(n_feat, n_feat, bias=False)
    # these two learnable bias are used in matrix c and matrix d
    # as described in https://arxiv.org/abs/1901.02860 Section 3.3
    self.do_rel_shift = do_rel_shift
    self.pos_bias_u = nn.Parameter(torch.Tensor(self.h, self.d_k))
    self.pos_bias_v = nn.Parameter(torch.Tensor(self.h, self.d_k))
    torch.nn.init.xavier_uniform_(self.pos_bias_u)
    torch.nn.init.xavier_uniform_(self.pos_bias_v)
    self.adaptive_scale = adaptive_scale
    self.ada_scale = nn.Parameter(torch.ones([1, 1, n_feat]),
                                  requires_grad=adaptive_scale)
    self.ada_bias = nn.Parameter(torch.zeros([1, 1, n_feat]),
                                 requires_grad=adaptive_scale)
    if init_weights:
        self.init_weights()


def init_weights(self):
    input_max = (self.h * self.d_k) ** -0.5
    torch.nn.init.uniform_(self.linear_q.weight, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_q.bias, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_k.weight, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_k.bias, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_v.weight, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_v.bias, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_pos.weight, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_out.weight, -input_max, input_max)
    torch.nn.init.uniform_(self.linear_out.bias, -input_max, input_max)


def rel_shift(self, x, zero_triu: bool = False):
    """Compute relative positinal encoding.
    Args:
        x (torch.Tensor): Input tensor (batch, time, size).
        zero_triu (bool): If true, return the lower triangular part of
            the matrix.
    Returns:
        torch.Tensor: Output tensor.
    """

    zero_pad = torch.zeros((x.size()[0], x.size()[1], x.size()[2], 1),
                           device=x.device,
                           dtype=x.dtype)
    x_padded = torch.cat([zero_pad, x], dim=-1)

    x_padded = x_padded.view(x.size()[0],
                             x.size()[1],
                             x.size(3) + 1, x.size(2))
    x = x_padded[:, :, 1:].view_as(x)

    if zero_triu:
        ones = torch.ones((x.size(2), x.size(3)))
        x = x * torch.tril(ones, x.size(3) - x.size(2))[None, None, :, :]

    return x


def forward_attention(
        self,
        value: torch.Tensor,
        scores: torch.Tensor,
        mask: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool)
) -> torch.Tensor:
    """Compute attention context vector.

    Args:
        value (torch.Tensor): Transformed value, size
            (#batch, n_head, time2, d_k).
        scores (torch.Tensor): Attention score, size
            (#batch, n_head, time1, time2).
        mask (torch.Tensor): Mask, size (#batch, 1, time2) or
            (#batch, time1, time2), (0, 0, 0) means fake mask.

    Returns:
        torch.Tensor: Transformed value (#batch, time1, d_model)
            weighted by the attention score (#batch, time1, time2).

    """
    n_batch = value.size(0)
    # NOTE(xcsong): When will `if mask.size(2) > 0` be True?
    #   1. onnx(16/4) [WHY? Because we feed real cache & real mask for the
    #           1st chunk to ease the onnx export.]
    #   2. pytorch training
    if mask.size(2) > 0:  # time2 > 0
        mask = mask.unsqueeze(1).eq(0)  # (batch, 1, *, time2)
        # For last chunk, time2 might be larger than scores.size(-1)
        mask = mask[:, :, :, :scores.size(-1)]  # (batch, 1, *, time2)
        scores = scores.masked_fill(mask, -float('inf'))
        # (batch, head, time1, time2)
        attn = torch.softmax(scores, dim=-1).masked_fill(mask, 0.0)
    # NOTE(xcsong): When will `if mask.size(2) > 0` be False?
    #   1. onnx(16/-1, -1/-1, 16/0)
    #   2. jit (16/-1, -1/-1, 16/0, 16/4)
    else:
        attn = torch.softmax(scores, dim=-1)  # (batch, head, time1, time2)

    p_attn = self.dropout(attn)
    x = torch.matmul(p_attn, value)  # (batch, head, time1, d_k)
    x = (x.transpose(1, 2).contiguous().view(n_batch, -1,
                                             self.h * self.d_k)
         )  # (batch, time1, d_model)

    return self.linear_out(x)  # (batch, time1, d_model)


def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        mask: torch.Tensor = torch.ones((0, 0, 0), dtype=torch.bool),
        pos_emb: torch.Tensor = torch.empty(0),
        cache: torch.Tensor = torch.zeros((0, 0, 0, 0))
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Compute 'Scaled Dot Product Attention' with rel. positional encoding.
    Args:
        query (torch.Tensor): Query tensor (#batch, time1, size).
        key (torch.Tensor): Key tensor (#batch, time2, size).
        value (torch.Tensor): Value tensor (#batch, time2, size).
        mask (torch.Tensor): Mask tensor (#batch, 1, time2) or
            (#batch, time1, time2), (0, 0, 0) means fake mask.
        pos_emb (torch.Tensor): Positional embedding tensor
            (#batch, time2, size).
        cache (torch.Tensor): Cache tensor (1, head, cache_t, d_k * 2),
            where `cache_t == chunk_size * num_decoding_left_chunks`
            and `head * d_k == size`
    Returns:
        torch.Tensor: Output tensor (#batch, time1, d_model).
        torch.Tensor: Cache tensor (1, head, cache_t + time1, d_k * 2)
            where `cache_t == chunk_size * num_decoding_left_chunks`
            and `head * d_k == size`
    """
    if self.adaptive_scale:
        query = self.ada_scale * query + self.ada_bias
        key = self.ada_scale * key + self.ada_bias
        value = self.ada_scale * value + self.ada_bias
    q, k, v = self.forward_qkv(query, key, value)
    q = q.transpose(1, 2)  # (batch, time1, head, d_k)

    # NOTE(xcsong):
    #   when export onnx model, for 1st chunk, we feed
    #       cache(1, head, 0, d_k * 2) (16/-1, -1/-1, 16/0 mode)
    #       or cache(1, head, real_cache_t, d_k * 2) (16/4 mode).
    #       In all modes, `if cache.size(0) > 0` will alwayse be `True`
    #       and we will always do splitting and
    #       concatnation(this will simplify onnx export). Note that
    #       it's OK to concat & split zero-shaped tensors(see code below).
    #   when export jit  model, for 1st chunk, we always feed
    #       cache(0, 0, 0, 0) since jit supports dynamic if-branch.
    # >>> a = torch.ones((1, 2, 0, 4))
    # >>> b = torch.ones((1, 2, 3, 4))
    # >>> c = torch.cat((a, b), dim=2)
    # >>> torch.equal(b, c)        # True
    # >>> d = torch.split(a, 2, dim=-1)
    # >>> torch.equal(d[0], d[1])  # True
    if cache.size(0) > 0:
        key_cache, value_cache = torch.split(cache,
                                             cache.size(-1) // 2,
                                             dim=-1)
        k = torch.cat([key_cache, k], dim=2)
        v = torch.cat([value_cache, v], dim=2)
    # NOTE(xcsong): We do cache slicing in encoder.forward_chunk, since it's
    #   non-trivial to calculate `next_cache_start` here.
    new_cache = torch.cat((k, v), dim=-1)

    n_batch_pos = pos_emb.size(0)
    p = self.linear_pos(pos_emb).view(n_batch_pos, -1, self.h, self.d_k)
    p = p.transpose(1, 2)  # (batch, head, time1, d_k)

    # (batch, head, time1, d_k)
    q_with_bias_u = (q + self.pos_bias_u).transpose(1, 2)
    # (batch, head, time1, d_k)
    q_with_bias_v = (q + self.pos_bias_v).transpose(1, 2)

    # compute attention score
    # first compute matrix a and matrix c
    # as described in https://arxiv.org/abs/1901.02860 Section 3.3
    # (batch, head, time1, time2)
    matrix_ac = torch.matmul(q_with_bias_u, k.transpose(-2, -1))

    # compute matrix b and matrix d
    # (batch, head, time1, time2)
    matrix_bd = torch.matmul(q_with_bias_v, p.transpose(-2, -1))
    # Remove rel_shift since it is useless in speech recognition,
    # and it requires special attention for streaming.
    if self.do_rel_shift:
        matrix_bd = self.rel_shift(matrix_bd)

    scores = (matrix_ac + matrix_bd) / math.sqrt(
        self.d_k)  # (batch, head, time1, time2)

    return self.forward_attention(v, scores, mask), new_cache