import torch
import torch.nn as nn


class SimpleNonlinearRegressor(nn.Module):
    def __init__(self, input_dim=3, output_dim=1):
        super().__init__()
        # 定义隐藏层：非线性变换的关键组件
        self.hidden_layer = nn.Linear(input_dim, 3)  # 32个隐藏单元
        self.relu = nn.LeakyReLU()  # 非线性激活函数
        self.output_layer = nn.Linear(3, output_dim)  # 回归输出层
    def forward(self, x):
        # x形状：[batch_size, seq_len=30, features=3]
        orig_shape = x.shape
        # 合并批次和时间维度：将(128,30,3)转换为(128*30,3)
        x = x.reshape(-1, orig_shape[-1])
        # 非线性变换过程
        x = self.hidden_layer(x)  # 线性变换
        x = self.relu(x)  # 非线性激活
        x = self.output_layer(x)  # 最终输出
        # 恢复原始维度结构：将(128*30,1)恢复为(128,30,1)
        return x.view(orig_shape[0], orig_shape[1], -1)


# 验证模型结构
if __name__ == "__main__":
    # 实例化模型
    model = SimpleNonlinearRegressor()

    # 创建模拟输入数据（128个样本，30时间步，3个特征）
    dummy_input = torch.randn(128, 30, 3)

    # 前向传播验证
    output = model(dummy_input)
    print(f"输入形状：{dummy_input.shape}")
    print(f"输出形状：{output.shape}")  # 应输出 torch.Size([128, 30, 1])
