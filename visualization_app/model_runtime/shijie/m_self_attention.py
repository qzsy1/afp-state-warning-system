import torch
import torch.nn as nn


class SelfAttentionBlock(nn.Module):
    def __init__(self, input_dim, num_heads):
        super().__init__()
        assert input_dim % num_heads == 0, "input_dim必须能被num_heads整除"

        # 定义多头自注意力层
        self.multihead_attn = nn.MultiheadAttention(
            embed_dim=input_dim,
            num_heads=num_heads,
            batch_first=True  # 输入格式为(batch, seq, feature)
        )

        # 可选的层归一化
        self.norm = nn.LayerNorm(input_dim)

    def forward(self, x):
        """
        输入形状: (batch_size, seq_len, input_dim)
        输出形状: (batch_size, seq_len, input_dim)
        """
        # 自注意力计算（q, k, v都使用同一输入）
        attn_output, _ = self.multihead_attn(x, x, x)

        # 残差连接 + 层归一化（可选）
        output = self.norm(x + attn_output)

        return output


# 使用示例
if __name__ == "__main__":
    # 模拟输入数据 [batch_size, window_length, features]
    batch_size = 128
    tcn_output = torch.randn(batch_size, 30, 14)

    # 初始化自注意力模块
    self_attention = SelfAttentionBlock(input_dim=14, num_heads=7)

    # 前向传播
    output = self_attention(tcn_output)

    # 验证输出形状
    print("输入形状:", tcn_output.shape)  # torch.Size([128, 30, 14])
    print("输出形状:", output.shape)  # torch.Size([128, 30, 14])