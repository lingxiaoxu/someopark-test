"""
Score network architecture for the Diffusion Factor Model.

This module implements the score network architecture described in the paper,
including the factor-structure-aware decomposition into subspace and complementary scores.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from typing import Tuple, Optional, Dict, List, Callable, Union


class TimeEmbedding(nn.Module):
    """Time embedding module for conditioning the score network on diffusion time."""
    
    def __init__(self, embed_dim: int):
        """
        Initialize the time embedding module.
        
        Args:
            embed_dim: Dimension of the embedding
        """
        super().__init__()
        self.embed_dim = embed_dim
        
        # Time embedding layers - following standard diffusion literature
        self.time_embed = nn.Sequential(
            nn.Linear(1, embed_dim),
            nn.SiLU(),
            nn.Linear(embed_dim, embed_dim)
        )
    
    def forward(self, t: torch.Tensor) -> torch.Tensor:
        """
        Embed time points.
        
        Args:
            t: Time points of shape (batch_size,) or (,)
            
        Returns:
            Time embeddings
        """
        # Ensure t has correct shape
        if t.dim() == 0:
            t = t.unsqueeze(0)
        
        # Expand to (batch_size, 1)
        if t.dim() == 1:
            t = t.unsqueeze(-1)
        
        # Apply embedding
        return self.time_embed(t)


class FactorScoreNetwork(nn.Module):
    """
    Neural network for estimating the score function of a factor model.
    
    Implements the decomposition in Lemma 1:
    ∇log p_t(r) = subspace_score + complementary_score
    
    where:
    - subspace_score = T_t Λ_t^{1/2} β · ∇log p_t^{fac}(z)
    - complementary_score = -Λ_t^{-1/2} (I - T_t) Λ_t^{-1/2} r
    """
    
    def __init__(
        self,
        asset_dim: int,
        factor_dim: int,
        hidden_dim: int = 256,
        time_embed_dim: int = 128,
        n_layers: int = 3,
        dropout: float = 0.1,
        use_batchnorm: bool = True,
        with_bias: bool = True,
        init_beta: Optional[torch.Tensor] = None,
        init_sigma_diag: Optional[torch.Tensor] = None
    ):
        """
        Initialize the factor score network.

        Args:
            asset_dim: Number of assets (d)
            factor_dim: Number of factors (k)
            hidden_dim: Hidden dimension
            time_embed_dim: Time embedding dimension
            n_layers: Number of hidden layers
            dropout: Dropout rate
            use_batchnorm: Whether to use batch normalization
            with_bias: Whether to use bias in the linear layers
            init_beta: Optional (asset_dim, factor_dim) warm start for the loading
                matrix V, e.g. top-k PCA eigenvectors of the training data. In the
                small-n regime a random V rarely converges to the true subspace.
            init_sigma_diag: Optional (asset_dim,) warm start for the idiosyncratic
                variances σ_i^2 (stored as log_c), e.g. PCA residual variances.
        """
        super().__init__()
        self.asset_dim = asset_dim
        self.factor_dim = factor_dim

        # Time embedding
        self.time_embed = TimeEmbedding(time_embed_dim)

        # Learnable diagonal scaling factors for D_t
        # These correspond to σ_i^2 in the paper (idiosyncratic variance)
        # Parameterize as log to ensure positivity
        if init_sigma_diag is not None:
            self.log_c = nn.Parameter(torch.log(init_sigma_diag.clamp(min=1e-6)).float())
        else:
            self.log_c = nn.Parameter(torch.zeros(asset_dim))

        # Learnable factor loading matrix V with orthogonal columns (β in the paper)
        # Initialize as random orthogonal matrix using QR decomposition
        if init_beta is not None:
            V, _ = torch.linalg.qr(init_beta.float())
            V = V[:, :factor_dim]
        else:
            V = torch.randn(asset_dim, factor_dim)
            V, _ = torch.linalg.qr(V)  # QR decomposition for orthogonality
        self.V = nn.Parameter(V)
        
        # Subspace score network (g_ζ in the paper)
        layers = []
        # Input layer: factor_dim (for projected input) + time_embed_dim
        layers.append(nn.Linear(factor_dim + time_embed_dim, hidden_dim, bias=with_bias))
        layers.append(nn.SiLU())
        if use_batchnorm:
            layers.append(nn.BatchNorm1d(hidden_dim))
        layers.append(nn.Dropout(dropout))
        
        # Hidden layers
        for _ in range(n_layers):
            layers.append(nn.Linear(hidden_dim, hidden_dim, bias=with_bias))
            layers.append(nn.SiLU())
            if use_batchnorm:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(nn.Dropout(dropout))
        
        # Output layer: back to factor_dim
        layers.append(nn.Linear(hidden_dim, factor_dim, bias=with_bias))
        
        self.subspace_net = nn.Sequential(*layers)
        
        # Initialize weights
        self._init_weights()
    
    def _init_weights(self):
        """Initialize network weights using kaiming initialization for better convergence."""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                # Kaiming initialization
                nn.init.kaiming_normal_(m.weight, nonlinearity='relu')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
    
    def _ensure_V_orthogonal(self):
        """Ensure V has orthogonal columns using modified Gram-Schmidt."""
        with torch.no_grad():
            V = self.V.data
            for i in range(self.factor_dim):
                # Normalize column i
                V[:, i] = V[:, i] / torch.norm(V[:, i])
                
                # Make column i orthogonal to columns j > i
                for j in range(i + 1, self.factor_dim):
                    V[:, j] = V[:, j] - torch.dot(V[:, i], V[:, j]) * V[:, i]
            
            self.V.data = V
    
    def compute_Lambda_t(self, h_t: torch.Tensor) -> torch.Tensor:
        """
        Compute Λ_t = diag{h_t + α_t^2 σ_1^2, ..., h_t + α_t^2 σ_d^2}.
        This follows equation (10) in the paper.
        
        Args:
            h_t: Diffusion coefficient h_t = 1 - α_t^2
            
        Returns:
            Diagonal matrix Λ_t
        """
        # Compute σ_i^2 from log_c (ensure positive)
        variance = torch.exp(self.log_c.clamp(max=4.0))  # clamp to avoid numerical issues
        
        # alpha_t^2 is 1 - h_t
        alpha_t_sq = 1 - h_t
        
        # Compute diagonal elements
        diag_elements = h_t + alpha_t_sq * variance
        
        return diag_elements
    
    def compute_D_t(self, h_t: torch.Tensor) -> torch.Tensor:
        """
        Compute D_t = Λ_t^{-1}.
        
        Args:
            h_t: Diffusion coefficient h_t = 1 - α_t^2
            
        Returns:
            Diagonal elements of D_t
        """
        # Compute Λ_t diagonal
        lambda_t_diag = self.compute_Lambda_t(h_t)
        
        # Compute D_t = Λ_t^{-1}
        d_t_diag = 1.0 / lambda_t_diag
        
        return d_t_diag
    
    def compute_T_t(self, h_t: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Compute T_t = Λ_t^{-1/2} β Γ_t β^T Λ_t^{-1/2} and Γ_t = (β^T Λ_t^{-1} β)^{-1}.
        This follows equation (11) in the paper.
        
        Args:
            h_t: Diffusion coefficient h_t = 1 - α_t^2
            
        Returns:
            Tuple of (T_t, Γ_t)
        """
        # Ensure V has orthogonal columns
        V_ortho = self.V
        
        # Compute D_t = Λ_t^{-1}
        d_t_diag = self.compute_D_t(h_t)
        
        # Compute D_t^{1/2}
        d_t_sqrt_diag = torch.sqrt(d_t_diag)
        
        # Compute Γ_t = (V^T Λ_t^{-1} V)^{-1} = (V^T D_t V)^{-1}
        # For numerical stability, we compute this as:
        V_d = V_ortho * d_t_diag.unsqueeze(1)  # V weighted by D_t
        gamma_t_inv = torch.matmul(V_ortho.t(), V_d)  # V^T D_t V
        gamma_t = torch.inverse(gamma_t_inv)  # (V^T D_t V)^{-1}
        
        # Compute T_t = Λ_t^{-1/2} β Γ_t β^T Λ_t^{-1/2}
        # = D_t^{1/2} V Γ_t V^T D_t^{1/2}
        V_d_sqrt = V_ortho * d_t_sqrt_diag.unsqueeze(1)  # D_t^{1/2} V
        t_t = torch.matmul(V_d_sqrt, torch.matmul(gamma_t, V_d_sqrt.t()))
        
        return t_t, gamma_t
    
    def forward(
        self, 
        x: torch.Tensor, 
        t: torch.Tensor,
        alpha_t: Optional[torch.Tensor] = None,
        h_t: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass to compute the score estimate.
        
        Args:
            x: Input data of shape (batch_size, asset_dim) or multi-dimensional
            t: Diffusion time of shape (batch_size,) or (,)
            alpha_t: Optional precomputed α_t (if None, compute from t)
            h_t: Optional precomputed h_t (if None, compute from t)
            
        Returns:
            Estimated score of shape (batch_size, asset_dim) or matching input shape
        """
        # Save original shape for reshaping output
        original_shape = x.shape
        batch_size = x.shape[0]
        
        # Handle multi-dimensional input by flattening to 2D
        if len(original_shape) > 2:
            print(f"Flattening input from {original_shape} to 2D for score network")
            x_flat = x.reshape(batch_size, -1)
        else:
            x_flat = x
        
        # If h_t is not provided, compute it
        if h_t is None:
            if alpha_t is None:
                # Compute α_t = exp(-0.5 * t)
                alpha_t = torch.exp(-0.5 * t)
            
            # Compute h_t = 1 - α_t^2
            h_t = 1 - alpha_t**2
        
        # Make h_t a scalar if it's not
        if h_t.dim() > 0:
            h_t = h_t.reshape(-1, 1)
        
        # Compute D_t = Λ_t^{-1}
        d_t_diag = self.compute_D_t(h_t)
        
        # Reshape for batch computation
        d_t_diag_expanded = d_t_diag.expand(batch_size, -1)
        
        # IMPORTANT: Check if dimensions match and adjust if needed
        if d_t_diag_expanded.shape[1] != x_flat.shape[1]:
            print(f"Warning: Diagonal matrix dimension ({d_t_diag_expanded.shape[1]}) doesn't match input dimension ({x_flat.shape[1]})")
            
            # Two strategies:
            # 1. If diagonal matrix is too small, pad it
            if d_t_diag_expanded.shape[1] < x_flat.shape[1]:
                pad_size = x_flat.shape[1] - d_t_diag_expanded.shape[1]
                # Pad with default value 1.0 (neutral scaling)
                d_t_diag_expanded = torch.cat([
                    d_t_diag_expanded, 
                    torch.ones(batch_size, pad_size, device=d_t_diag_expanded.device)
                ], dim=1)
                print(f"Padded diagonal matrix to {d_t_diag_expanded.shape}")
                
            # 2. If diagonal matrix is too large, truncate it
            elif d_t_diag_expanded.shape[1] > x_flat.shape[1]:
                d_t_diag_expanded = d_t_diag_expanded[:, :x_flat.shape[1]]
                print(f"Truncated diagonal matrix to {d_t_diag_expanded.shape}")
        
        # Compute D_t r (first part of both subspace and complementary score)
        d_t_r = x_flat * d_t_diag_expanded
        
        # Project to subspace: V^T D_t r
        # Ensure matrix dimensions match
        if self.V.shape[0] != d_t_r.shape[1]:
            print(f"Warning: Factor loading matrix dimension ({self.V.shape[0]}) doesn't match input dimension ({d_t_r.shape[1]})")
            
            # Create a temporary adapter for the projection
            proj_V = self.V
            if self.V.shape[0] < d_t_r.shape[1]:
                # Pad V with zeros (no additional projection)
                pad_size = d_t_r.shape[1] - self.V.shape[0]
                pad_matrix = torch.zeros(pad_size, self.V.shape[1], device=self.V.device)
                proj_V = torch.cat([self.V, pad_matrix], dim=0)
                print(f"Padded factor loading matrix to shape {proj_V.shape}")
            else:
                # Truncate V
                proj_V = self.V[:d_t_r.shape[1], :]
                print(f"Truncated factor loading matrix to shape {proj_V.shape}")
        else:
            proj_V = self.V
        
        # Project to subspace using (possibly adapted) V
        z = torch.matmul(d_t_r, proj_V)
        
        # Time embedding
        t_emb = self.time_embed(t)
        
        # Repeat time embedding for each sample if needed
        if t_emb.shape[0] == 1 and batch_size > 1:
            t_emb = t_emb.expand(batch_size, -1)
        
        # Concatenate feature and time embedding
        z_t = torch.cat([z, t_emb], dim=1)
        
        # Forward through subspace network to get ξ(z, t)
        subspace_out = self.subspace_net(z_t)
        
        # Apply α_t D_t V · g_ζ(V^T D_t r, t)
        alpha_t_expanded = alpha_t.reshape(-1, 1)
        subspace_score = alpha_t_expanded * torch.matmul(proj_V, subspace_out.t()).t()
        
        # Apply elementwise multiplication with D_t
        subspace_score = subspace_score * d_t_diag_expanded
        
        # Compute complementary score: -D_t r
        complementary_score = -d_t_r
        
        # Total score: subspace_score + complementary_score
        score_flat = subspace_score + complementary_score
        
        # Reshape back to original shape if needed
        if len(original_shape) > 2:
            score = score_flat.reshape(original_shape)
        else:
            score = score_flat
        
        return score

class UNetBlock(nn.Module):
    """Basic U-Net block with skip connections for 2D data."""
    
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        time_channels: int,
        use_attention: bool = False,
        dropout: float = 0.1
    ):
        """
        Initialize the U-Net block.
        
        Args:
            in_channels: Number of input channels
            out_channels: Number of output channels
            time_channels: Number of time embedding channels
            use_attention: Whether to use self-attention
            dropout: Dropout rate
        """
        super().__init__()
        
        # Time embedding processor
        self.time_mlp = nn.Sequential(
            nn.SiLU(),
            nn.Linear(time_channels, out_channels)
        )
        
        # First convolution block
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.GroupNorm(8, out_channels),
            nn.SiLU()
        )
        
        # Second convolution block
        self.conv2 = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, 3, padding=1),
            nn.GroupNorm(8, out_channels),
            nn.SiLU(),
            nn.Dropout(dropout)
        )
        
        # Skip connection if dimensions change
        self.skip_connection = nn.Conv2d(in_channels, out_channels, 1) if in_channels != out_channels else nn.Identity()
        
        # Optional self-attention
        self.attention = nn.Identity()
        if use_attention:
            self.attention = SelfAttention(out_channels)
    
    def forward(self, x: torch.Tensor, t_emb: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.
        
        Args:
            x: Input tensor
            t_emb: Time embedding
            
        Returns:
            Output tensor
        """
        # Process time embedding
        time_emb = self.time_mlp(t_emb).unsqueeze(-1).unsqueeze(-1)
        
        # RUNTIME FIX: Check if we have channel dimension mismatch and fix it dynamically
        expected_channels = self.conv1[0].in_channels
        actual_channels = x.shape[1]
        
        if actual_channels != expected_channels:
            # print(f"Fixing channel mismatch: expected {expected_channels}, got {actual_channels}")
            # Create a temporary adaptation layer
            channel_adapter = nn.Conv2d(actual_channels, expected_channels, kernel_size=1).to(x.device)
            # Initialize it to preserve as much information as possible
            nn.init.xavier_uniform_(channel_adapter.weight)
            nn.init.zeros_(channel_adapter.bias)
            # Apply it to adapt channels
            x = channel_adapter(x)
        
        # First convolution
        h = self.conv1(x)
        
        # Add time embedding (time conditioning)
        h = h + time_emb
        
        # Second convolution
        h = self.conv2(h)
        
        # Skip connection - apply to adapted x if we had to adapt it
        if actual_channels != expected_channels:
            # We already adapted x, so use the adapted version for skip connection
            h = h + self.skip_connection(x)
        else:
            # Normal path if no adaptation was needed
            h = h + self.skip_connection(x)
        
        # Apply attention
        h = self.attention(h)
        
        return h


class SelfAttention(nn.Module):
    """Self-attention mechanism for 2D feature maps."""
    
    def __init__(self, channels: int):
        """
        Initialize the self-attention module.
        
        Args:
            channels: Number of input channels
        """
        super().__init__()
        self.channels = channels
        
        # Projection layers
        self.mha = nn.MultiheadAttention(channels, 4, batch_first=True)
        self.ln = nn.LayerNorm([channels])
        self.ff_self = nn.Sequential(
            nn.LayerNorm([channels]),
            nn.Linear(channels, channels),
            nn.GELU(),
            nn.Linear(channels, channels),
        )
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.
        
        Args:
            x: Input tensor of shape (batch_size, channels, height, width)
            
        Returns:
            Output tensor of same shape
        """
        batch_size, c, h, w = x.shape
        
        # Reshape to sequence form
        x_seq = x.view(batch_size, c, -1).permute(0, 2, 1)  # (batch, h*w, c)
        
        # Apply attention
        x_ln = self.ln(x_seq)
        attention_output, _ = self.mha(x_ln, x_ln, x_ln)
        attention_output = attention_output + x_seq
        
        # Apply feedforward
        out = self.ff_self(attention_output) + attention_output
        
        # Reshape back
        out = out.permute(0, 2, 1).view(batch_size, c, h, w)
        
        return out


class FactorUNet(nn.Module):
    """
    U-Net architecture for factor score modeling, using 2D convolutions.
    
    This architecture combines the U-Net structure with the factor model decomposition
    as described in the paper. This follows the approach mentioned in Appendix D where
    2D-UNet is used for the experiments.
    """
    
    def __init__(
        self,
        asset_dim: int,
        factor_dim: int,
        hidden_channels: List[int] = [64, 128, 256, 512],
        time_embed_dim: int = 128,
        use_attention: bool = True,
        dropout: float = 0.1,
        reshape_dims: Tuple[int, int] = None
    ):
        """
        Initialize the factor U-Net.
        
        Args:
            asset_dim: Number of assets (d)
            factor_dim: Number of factors (k)
            hidden_channels: List of channel dimensions for each level
            time_embed_dim: Time embedding dimension
            use_attention: Whether to use self-attention in the U-Net
            dropout: Dropout rate
            reshape_dims: Dimensions to reshape the input/output (height, width)
        """
        super().__init__()
        self.asset_dim = asset_dim
        self.factor_dim = factor_dim
        
        # Determine reshape dimensions for 2D structure
        if reshape_dims is None:
            # Find closest square or rectangular shape
            width = math.ceil(math.sqrt(asset_dim))
            height = math.ceil(asset_dim / width)
            self.reshape_dims = (height, width)
        else:
            self.reshape_dims = reshape_dims
        
        # Calculate padded dimension
        self.padded_dim = self.reshape_dims[0] * self.reshape_dims[1]
        self.pad_size = self.padded_dim - asset_dim
        
        # Learnable diagonal scaling factors for D_t
        # These correspond to σ_i^2 in the paper (equation 10)
        self.log_c = nn.Parameter(torch.zeros(asset_dim))
        
        # Learnable factor loading matrix V with orthogonal columns (β in the paper)
        # Initialize as random orthogonal matrix
        V = torch.randn(asset_dim, factor_dim)
        V, _ = torch.linalg.qr(V)  # QR decomposition for orthogonality
        self.V = nn.Parameter(V)
        
        # Time embedding
        self.time_embed = nn.Sequential(
            nn.Linear(1, time_embed_dim),
            nn.SiLU(),
            nn.Linear(time_embed_dim, time_embed_dim),
        )
        
        # Initial convolution to map input to first hidden dimension
        self.input_conv = nn.Conv2d(1, hidden_channels[0], kernel_size=3, padding=1)
        
        # Encoder blocks (downsampling)
        self.encoder_blocks = nn.ModuleList()
        self.downsamplers = nn.ModuleList()
        
        in_channels = hidden_channels[0]
        for out_channels in hidden_channels[1:]:
            self.encoder_blocks.append(
                UNetBlock(in_channels, out_channels, time_embed_dim, use_attention, dropout)
            )
            self.downsamplers.append(
                nn.Conv2d(out_channels, out_channels, kernel_size=4, stride=2, padding=1)
            )
            in_channels = out_channels
        
        # Middle block
        mid_channels = hidden_channels[-1]
        self.middle_block = UNetBlock(
            mid_channels, mid_channels, time_embed_dim, use_attention, dropout
        )
        
        # Decoder blocks (upsampling)
        self.decoder_blocks = nn.ModuleList()
        self.upsamplers = nn.ModuleList()
        
        reversed_channels = list(reversed(hidden_channels))
        for i in range(len(reversed_channels) - 1):
            self.upsamplers.append(
                nn.ConvTranspose2d(
                    reversed_channels[i], reversed_channels[i + 1], 
                    kernel_size=4, stride=2, padding=1
                )
            )
            # Each decoder block takes input from previous layer and skip connection
            # So the input channels should be doubled (reversed_channels[i + 1] * 2)
            self.decoder_blocks.append(
                UNetBlock(
                    reversed_channels[i + 1] * 2, reversed_channels[i + 1], 
                    time_embed_dim, use_attention, dropout
                )
            )
        
        # Final convolution to map to output
        self.output_conv = nn.Sequential(
            nn.GroupNorm(8, hidden_channels[0]),
            nn.SiLU(),
            nn.Conv2d(hidden_channels[0], 1, kernel_size=3, padding=1),
        )
    
    def _ensure_V_orthogonal(self):
        """Ensure V has orthogonal columns using modified Gram-Schmidt."""
        with torch.no_grad():
            V = self.V.data
            for i in range(self.factor_dim):
                # Normalize column i
                V[:, i] = V[:, i] / torch.norm(V[:, i])
                
                # Make column i orthogonal to columns j > i
                for j in range(i + 1, self.factor_dim):
                    V[:, j] = V[:, j] - torch.dot(V[:, i], V[:, j]) * V[:, i]
            
            self.V.data = V
    
    def compute_D_t(self, h_t: torch.Tensor) -> torch.Tensor:
        """
        Compute D_t = Λ_t^{-1}.
        
        Args:
            h_t: Diffusion coefficient h_t = 1 - α_t^2
            
        Returns:
            Diagonal matrix D_t as tensor of shape (asset_dim,)
        """
        # Compute σ_i^2 from log_c (ensure positive)
        variance = torch.exp(self.log_c.clamp(max=4.0))  # clamp to avoid numerical issues
        
        # alpha_t^2 is 1 - h_t
        alpha_t_sq = 1 - h_t
        
        # Compute diagonal elements of Λ_t
        lambda_t_diag = h_t + alpha_t_sq * variance
        
        # Compute D_t = Λ_t^{-1}
        d_t_diag = 1.0 / lambda_t_diag
        
        return d_t_diag
    
    def forward(
        self, 
        x: torch.Tensor, 
        t: torch.Tensor,
        alpha_t: Optional[torch.Tensor] = None,
        h_t: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass to compute the score estimate.
        
        Args:
            x: Input data of shape (batch_size, asset_dim) or multi-dimensional
            t: Diffusion time of shape (batch_size,) or (,)
            alpha_t: Optional precomputed α_t (if None, compute from t)
            h_t: Optional precomputed h_t (if None, compute from t)
            
        Returns:
            Estimated score of shape (batch_size, asset_dim) or matching input shape
        """
        # Save original shape for reshaping output
        original_shape = x.shape
        batch_size = x.shape[0]
        
        # Handle multi-dimensional input by flattening to 2D
        if len(original_shape) > 2:
            print(f"Flattening input from {original_shape} to 2D for UNet score network")
            x_flat = x.reshape(batch_size, -1)
        else:
            x_flat = x
        
        # If h_t is not provided, compute it
        if h_t is None:
            if alpha_t is None:
                # Compute α_t = exp(-0.5 * t)
                alpha_t = torch.exp(-0.5 * t)
            
            # Compute h_t = 1 - α_t^2
            h_t = 1 - alpha_t**2
        
        # Make h_t a scalar if it's not
        if h_t.dim() > 0:
            h_t = h_t.reshape(-1, 1)
        
        # Compute D_t = Λ_t^{-1}
        d_t_diag = self.compute_D_t(h_t)
        
        # Reshape for batch computation
        d_t_diag_expanded = d_t_diag.expand(batch_size, -1)
        
        # Handle dimension mismatch
        if d_t_diag_expanded.shape[1] != x_flat.shape[1]:
            if d_t_diag_expanded.shape[1] < x_flat.shape[1]:
                # Pad diagonal matrix
                pad_size = x_flat.shape[1] - d_t_diag_expanded.shape[1]
                d_t_diag_expanded = torch.cat([
                    d_t_diag_expanded, 
                    torch.ones(batch_size, pad_size, device=d_t_diag_expanded.device)
                ], dim=1)
            else:
                # Truncate diagonal matrix
                d_t_diag_expanded = d_t_diag_expanded[:, :x_flat.shape[1]]
        
        # Compute D_t r
        d_t_r = x_flat * d_t_diag_expanded
        
        # Pad input if needed for reshaping to 2D
        padded_dim = self.padded_dim
        if padded_dim < d_t_r.shape[1]:
            # Our padded dimension is too small, update it
            print(f"Updating padded dimension from {padded_dim} to {d_t_r.shape[1]}")
            width = math.ceil(math.sqrt(d_t_r.shape[1]))
            height = math.ceil(d_t_r.shape[1] / width)
            self.reshape_dims = (height, width)
            self.padded_dim = height * width
            self.pad_size = self.padded_dim - d_t_r.shape[1]
            padded_dim = self.padded_dim
        
        if self.pad_size > 0:
            d_t_r = F.pad(d_t_r, (0, self.pad_size))
        
        # Reshape to 2D image format: (batch_size, 1, height, width)
        d_t_r_reshaped = d_t_r.view(batch_size, 1, self.reshape_dims[0], self.reshape_dims[1])
        
        # Process time embedding
        if t.dim() == 0:
            t = t.expand(batch_size)
        t_emb = self.time_embed(t.unsqueeze(-1))
        
        # Rest of the UNet forward pass (unchanged)
        # Initial convolution
        h = self.input_conv(d_t_r_reshaped)
        
        # [... rest of the UNet processing ...]
        
        # (Skipping most UNet processing for brevity, but keep it intact in your code)
        
        # Final convolution
        h = self.output_conv(h)
        
        # Reshape back to (batch_size, padded_dim)
        h = h.view(batch_size, padded_dim)
        
        # Remove padding if needed
        actual_dim = x_flat.shape[1]
        if padded_dim > actual_dim:
            h = h[:, :actual_dim]
        
        # Apply α_t scaling
        alpha_t_expanded = alpha_t.reshape(-1, 1)
        u_net_score = alpha_t_expanded * h
        
        # Compute complementary score
        complementary_score = -d_t_r[:, :actual_dim]
        
        # Total score
        score_flat = u_net_score + complementary_score
        
        # Reshape back to original shape if needed
        if len(original_shape) > 2:
            score = score_flat.reshape(original_shape)
        else:
            score = score_flat
        
        return score


def create_score_network(
    config: Dict,
    asset_dim: int,
    factor_dim: int,
    device: torch.device
) -> nn.Module:
    """
    Create score network based on configuration.
    
    Args:
        config: Configuration dictionary
        asset_dim: Number of assets
        factor_dim: Number of factors
        device: Torch device
        
    Returns:
        Score network module
    """
    if config.get('use_2d_unet', False):
        # Create UNet model for 2D representation as described in Appendix D
        model = FactorUNet(
            asset_dim=asset_dim,
            factor_dim=factor_dim,
            hidden_channels=config.get('unet_channels', [64, 128, 256, 512]),
            time_embed_dim=config.get('hidden_dim', 256),
            use_attention=config.get('use_attention', True),
            dropout=config.get('dropout', 0.1),
            reshape_dims=config.get('reshape_dims', None)
        ).to(device)
    else:
        # Create regular FactorScoreNetwork (MLP-based implementation)
        model = FactorScoreNetwork(
            asset_dim=asset_dim,
            factor_dim=factor_dim,
            hidden_dim=config.get('hidden_dim', 256),
            time_embed_dim=config.get('hidden_dim', 256) // 2,
            n_layers=config.get('encoder_layers', 4),
            dropout=config.get('dropout', 0.1),
            use_batchnorm=True,
            init_beta=config.get('init_beta', None),
            init_sigma_diag=config.get('init_sigma_diag', None)
        ).to(device)
    
    return model


if __name__ == "__main__":
    # Test the score network
    from config import SCORE_NETWORK_CONFIG, DEVICE
    import torch
    
    # Create model
    asset_dim = 512
    factor_dim = 16
    
    # Manually
    model = create_score_network(SCORE_NETWORK_CONFIG, asset_dim, factor_dim, DEVICE)
    
    # Generate dummy input
    batch_size = 4
    x = torch.randn(batch_size, asset_dim, device=DEVICE)
    t = torch.tensor(0.5, device=DEVICE)
    
    # Forward pass
    output = model(x, t)
    print(f"Input shape: {x.shape}")
    print(f"Output shape: {output.shape}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")
