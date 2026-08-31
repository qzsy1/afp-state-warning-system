import torch
import torch.nn as nn
import torch.nn.functional as F


class InteractiveAttention(nn.Module):
    def __init__(self, sensor_dim, condition_dim, hidden_dim):
        super().__init__()
        # 工况数据投影层
        self.W_cond = nn.Linear(condition_dim, 1)
        # 传感器数据投影层
        self.W_sensor = nn.Linear(sensor_dim, 6)
        # 注意力参数
        self.W_att = nn.Linear(hidden_dim, 1)

    def forward(self, sensor, condition):
        """
        sensor: [B,T,D_sensor] = [128,30,14]
        condition: [B,T,D_cond] = [128,30,1]
        """
        # print('sensor:'+str(sensor.shape))
        # sensor=sensor.permute(0,2,1)
        B, T, _ = sensor.shape
        sensor=sensor.to('cuda')
        condition=condition.to('cuda')
        # 工况数据全局特征（公式5改编）
        # h_cond = condition.mean(dim=1)  # [B,1]
        # h_cond = h_cond.unsqueeze(1).expand(-1, T, -1)  # [B,T,1]
        # h_cond = condition
        # 传感器时序特征提取（公式8改编）
        sensor_proj = self.W_sensor(sensor)  # [B,T,hidden_dim]
        # print('sensor_proj:'+str(sensor_proj.shape))
        cond_proj = self.W_cond(condition)  # [B,T,hidden_dim]
        # print('cond_proj:'+str(cond_proj.shape))
        # 交互式注意力计算（公式7改编）
        # s=torch.bmm(sensor_proj.permute(0,2,1), cond_proj)
        # print('s:'+str(s.shape))
        # energy = torch.tanh(s)  # [B,T,hidden_dim]
        energy = torch.einsum("bti,bti->bt", sensor_proj, cond_proj)  # [B,T]
        # energy=torch.tanh(energy)
        # print('energy:'+str(energy.shape))
        # att_scores = self.W_att(energy) # [B,T]
        # att_scores = att_scores.squeeze(-1)  # [B,T]
        # print('att_scores:'+str(att_scores.shape))
        att_weights = F.softmax(energy, dim=1)  # [B,T]
        att_weights = att_weights.squeeze(-1)  # [B,T]
        # att_weights = att_weights.permute(0, 2, 1)
        # print('att_weights:'+str(att_weights.shape))
        # print(sensor.shape)
        # 加权融合（公式6改编）
        # 扩展 att_weights 的维度以匹配 sensor_data
        att_weights_expanded = att_weights.unsqueeze(1)  # 形状变为 [128, 1, 14]
        sensor = sensor.permute(0, 2, 1)  # [B,D_sensor,T]
        # 执行元素级乘法（利用广播机制）
        weighted_sensor = sensor* att_weights_expanded
        # weighted_sensor = weighted_sensor.unsqueeze(1)  # [B,1,D_sensor]
        # print('weighted_sensor:'+str(weighted_sensor.shape))
        # 残差连接保留时序信息
        # updated_sensor = sensor + weighted_sensor.expand(-1, T, -1)

        return weighted_sensor  # [B,T,D_sensor]


class InteractiveAttention0(nn.Module):
    def __init__(self, sensor_dim, condition_dim, hidden_dim):
        super().__init__()
        # 工况数据投影层
        self.W_cond = nn.Linear(condition_dim, 1)
        # 传感器数据投影层
        self.W_sensor = nn.Linear(sensor_dim, sensor_dim)
        # 注意力参数
        self.W_att = nn.Linear(hidden_dim, 1)

    def forward(self, sensor, condition):
        """
        sensor: [B,T,D_sensor] = [128,30,14]
        condition: [B,T,D_cond] = [128,30,1]
        """
        # print('sensor:'+str(sensor.shape))
        # sensor=sensor.permute(0,2,1)
        B, T, _ = sensor.shape

        # 工况数据全局特征（公式5改编）
        # h_cond = condition.mean(dim=1)  # [B,1]
        # h_cond = h_cond.unsqueeze(1).expand(-1, T, -1)  # [B,T,1]
        # h_cond = condition
        # sensor=sensor.reshape(B, T, -1,1)
        # print('sensor:'+str(sensor.shape))
        # 传感器时序特征提取（公式8改编）
        sensor_proj = self.W_sensor(sensor)  # [B,T,hidden_dim]
        # print('sensor_proj:'+str(sensor_proj.shape))
        cond_proj = self.W_cond(condition)  # [B,T,hidden_dim]
        cond_proj=cond_proj.expand_as(sensor_proj)
        # print('cond_proj:'+str(cond_proj.shape))
        # 交互式注意力计算（公式7改编）
        # s=torch.bmm(sensor_proj.permute(0,2,1), cond_proj)
        # print('s:'+str(sensor_proj.shape))
        # energy = torch.tanh(s)  # [B,T,hidden_dim]
        energy = torch.einsum("bti,bti->bti",  cond_proj,sensor_proj)  # [B,T]
        # energy=torch.tanh(energy)
        # print('energy:'+str(energy.shape))
        # att_scores = self.W_att(energy) # [B,T]
        # att_scores = att_scores.squeeze(-1)  # [B,T]
        # print('att_scores:'+str(att_scores.shape))
        att_weights = F.softmax(energy, dim=1)  # [B,T]
        # print(att_weights.shape)
        # att_weights = att_weights.squeeze(-1)  # [B,T]

        # att_weights = att_weights.permute(0, 2, 1)
        # print('att_weights:'+str(att_weights.shape))
        # print(sensor.shape)
        # 加权融合（公式6改编）
        # 扩展 att_weights 的维度以匹配 sensor_data
        # att_weights_expanded = att_weights.unsqueeze(1)  # 形状变为 [128, 1, 14]
        # sensor = sensor.permute(0, 2, 1)  # [B,D_sensor,T]
        # print('att_weights_expanded:'+str(att_weights.shape))
        # print('sensor:'+str(sensor.shape))
        # 执行元素级乘法（利用广播机制）
        # weighted_sensor = sensor* att_weights
        weighted_sensor=torch.einsum("bti,bti->bti", att_weights, sensor)
        # weighted_sensor = weighted_sensor.unsqueeze(1)  # [B,1,D_sensor]
        # print('weighted_sensor:'+str(weighted_sensor.shape))
        # 残差连接保留时序信息
        # updated_sensor = sensor + weighted_sensor.expand(-1, T, -1)

        return weighted_sensor  # [B,T,D_sensor]


class InteractiveAttention1(nn.Module):
    def __init__(self, sensor_dim, condition_dim=1, hidden_dim=32):
        super().__init__()
        # 传感器特征变换
        self.W_sensor = nn.Linear(sensor_dim, hidden_dim)
        # 工况特征变换（扩展为与传感器相同的隐藏维度）
        self.W_cond = nn.Linear(condition_dim, hidden_dim)
        # 特征融合门控
        self.gate = nn.Linear(2 * hidden_dim, sensor_dim)

    def forward(self, sensor, condition):
        """
        sensor: [B,T,D_sensor] = [128,30,14]
        condition: [B,T,D_cond] = [128,30,1]
        """
        B, T, _ = sensor.shape

        # 逐时间步特征变换
        sensor_proj = self.W_sensor(sensor)  # [B,T,H]
        cond_proj = self.W_cond(condition)  # [B,T,H]

        # 交互特征计算
        combined = torch.cat([sensor_proj, cond_proj], dim=-1)  # [B,T,2H]
        gate = torch.sigmoid(self.gate(combined))  # [B,T,D_sensor]

        # 门控融合
        updated_sensor = sensor * gate  # [B,T,D_sensor]

        return updated_sensor
import torch
import torch.nn as nn
import torch.nn.functional as F

class InteractiveAttention_ai(nn.Module):
    def __init__(self, sensor_dim, condition_dim, hidden_dim=32):
        super().__init__()
        # 传感器特征变换
        self.W_sensor = nn.Linear(sensor_dim, hidden_dim)
        # 工况特征变换
        self.W_cond = nn.Linear(condition_dim, hidden_dim)
        # 特征融合门控 (输出维度对齐 sensor_dim)
        self.gate = nn.Linear(2 * hidden_dim, sensor_dim)

    def forward(self, sensor, condition):
        """
        sensor: [Batch, Seq_len, D_sensor]
        condition: [Batch, Seq_len, D_cond]
        """
        # 逐时间步特征变换
        sensor_proj = self.W_sensor(sensor)  # [B, T, hidden_dim]
        cond_proj = self.W_cond(condition)   # [B, T, hidden_dim]

        # 交互特征计算与门控生成 (使用 Sigmoid 将权重控制在 0~1 之间)
        combined = torch.cat([sensor_proj, cond_proj], dim=-1)  # [B, T, 2*hidden_dim]
        gate = torch.sigmoid(self.gate(combined))               # [B, T, D_sensor]

        # 🚨 核心修复：门控加权后，必须通过残差连接加上原信号！
        updated_sensor = sensor + sensor * gate                 # [B, T, D_sensor]

        return updated_sensor