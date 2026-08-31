import torch
from torch_geometric.nn import ChebConv
class GConvGRU(torch.nn.Module):
    r"""An implementation of the Chebyshev Graph Convolutional Gated Recurrent Unit
    Cell. For details see this paper: `"Structured Sequence Modeling with Graph
    Convolutional Recurrent Networks." <https://arxiv.org/abs/1612.07659>`_

    Args:
        in_channels (int): Number of input features.
        out_channels (int): Number of output features.
        K (int): Chebyshev filter size :math:`K`.
        normalization (str, optional): The normalization scheme for the graph 拉普拉斯矩阵标准化方法
            Laplacian (default: :obj:`"sym"`):

            1. :obj:`None`: No normalization 不标准化L=D-A
            :math:`\mathbf{L} = \mathbf{D} - \mathbf{A}`

            2. :obj:`"sym"`: Symmetric normalization 对称标准化L=I-D(-1/2)AD(-1/2)
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1/2} \mathbf{A}
            \mathbf{D}^{-1/2}`

            3. :obj:`"rw"`: Random-walk normalization 随机游走标准化L=I-D(-1)A
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1} \mathbf{A}`

            You need to pass :obj:`lambda_max` to the :meth:`forward` method of
            this operator in case the normalization is non-symmetric.
            :obj:`\lambda_max` should be a :class:`torch.Tensor` of size
            :obj:`[num_graphs]` in a mini-batch scenario and a
            scalar/zero-dimensional tensor when operating on single graphs.
            You can pre-compute :obj:`lambda_max` via the
            :class:`torch_geometric.transforms.LaplacianLambdaMax` transform.
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
    """

    def __init__(
            self,
            in_channels:int,
            out_channels: 14,
            K: int,
            normalization: str = "sym",
            bias: bool = True,
    ):
        super(GConvGRU, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.K = K
        self.normalization = normalization
        self.bias = bias
        self._create_parameters_and_layers()

    def _create_update_gate_parameters_and_layers(self):
        self.conv_x_z = ChebConv(
            in_channels=self.in_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

        self.conv_h_z = ChebConv(
            in_channels=self.out_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

    def _create_reset_gate_parameters_and_layers(self):
        self.conv_x_r = ChebConv(
            in_channels=self.in_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

        self.conv_h_r = ChebConv(
            in_channels=self.out_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

    def _create_candidate_state_parameters_and_layers(self):
        self.conv_x_h = ChebConv(
            in_channels=self.in_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

        self.conv_h_h = ChebConv(
            in_channels=self.out_channels,
            out_channels=self.out_channels,
            K=self.K,
            normalization=self.normalization,
            bias=self.bias,
        )

    def _create_parameters_and_layers(self):
        self._create_update_gate_parameters_and_layers()
        self._create_reset_gate_parameters_and_layers()
        self._create_candidate_state_parameters_and_layers()

    def _set_hidden_state(self, X, H):
         #64,30,15
        if H is None:
            H = torch.zeros(X.shape[0], self.out_channels).to(X.device)
            # print('h_shape:',H.shape)  #64,30
        return H

    def _calculate_update_gate(self, X, edge_index, H, lambda_max):
        Z1 = self.conv_x_z(X, edge_index, lambda_max=lambda_max)  # (x)*_g(w) 64,30,30,30
        # print('z1_shape:',Z1.shape)
        Z2 = self.conv_h_z(H, edge_index, lambda_max=lambda_max)  # (x)*_g(w)+(h_z)*_g(w)
        Z2 = Z2.unsqueeze(-1)
        Z2 = Z2.unsqueeze(-1)
        # print('z2_shape:',Z2.shape) #64,30,1,1
        Z = Z1 + Z2
        # print('z_shape:',Z.shape)
        Z = torch.sigmoid(Z)  # 64,30,30,30
        return Z

    def _calculate_reset_gate(self, X, edge_index, H, lambda_max):
        R1 = self.conv_x_r(X, edge_index, lambda_max=lambda_max)
        R2 = self.conv_h_r(H, edge_index, lambda_max=lambda_max)
        R2 = R2.unsqueeze(-1)
        R2 = R2.unsqueeze(-1)
        # print('r2_shape:',R2.shape) #64,30,1,1
        R = R1 + R2
        # print('r_shape:',R.shape)
        R = torch.sigmoid(R)  # 64,30,30,30
        return R

    def _calculate_candidate_state(self, X, edge_index, H, R, lambda_max):
        H = H.unsqueeze(-1)
        H = H.unsqueeze(-1)
        H_tilde1 = self.conv_x_h(X, edge_index, lambda_max=lambda_max)
        # print('H_tilde1_shape:',H_tilde1.shape) #64,30,30,30
        H_tilde2 = self.conv_h_h(H * R, edge_index, lambda_max=lambda_max)
        # print('H_tilde2_shape:',H_tilde2.shape) #64,30,30,30
        H_tilde = H_tilde1 + H_tilde2
        # print('H_tilde_shape:',H_tilde.shape) #64,30,30,30
        H_tilde = torch.tanh(H_tilde)
        return H_tilde

    def _calculate_hidden_state(self, Z, H, H_tilde):
        H = H.unsqueeze(-1)
        H = H.unsqueeze(-1)
        H = Z * H + (1 - Z) * H_tilde
        # print('H_shape:',H.shape) #64,30,30,30
        return H

    def forward(
            self,
            X: torch.FloatTensor,
            edge_index: torch.LongTensor,
            # edge_weight: torch.FloatTensor = None,
            H: torch.FloatTensor = None,
            lambda_max: torch.Tensor = None,
    ) -> torch.FloatTensor:
        """
        Making a forward pass. If edge weights are not present the forward pass
        defaults to an unweighted graph. If the hidden state matrix is not present
        when the forward pass is called it is initialized with zeros.

        Arg types:
            * **X** *(PyTorch Float Tensor)* - Node features.
            * **edge_index** *(PyTorch Long Tensor)* - Graph edge indices.
            * **edge_weight** *(PyTorch Long Tensor, optional)* - Edge weight vector.
            * **H** *(PyTorch Float Tensor, optional)* - Hidden state matrix for all nodes.
            * **lambda_max** *(PyTorch Tensor, optional but mandatory if normalization is not sym)* - Largest eigenvalue of Laplacian.


        Return types:
            * **H** *(PyTorch Float Tensor)* - Hidden state matrix for all nodes.
        """

        X = X.float()
        H = self._set_hidden_state(X, H)
        # print(X.shape,H.shape)
        Z = self._calculate_update_gate(X, edge_index, H, lambda_max)
        R = self._calculate_reset_gate(X, edge_index, H, lambda_max)
        H_tilde = self._calculate_candidate_state(X, edge_index, H, R, lambda_max)
        H = self._calculate_hidden_state(Z, H, H_tilde)  # 64，30，30，30
        # print('H:',H.shape)

        return H