"""
Score matching training process for the Diffusion Factor Model.

This module implements the score matching training process,
including loss functions and training loops.
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from typing import Dict, Optional, Callable, List, Tuple, Any
import time
import numpy as np
from tqdm import tqdm
import os
import logging

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ScoreMatchingLoss(nn.Module):
    """Loss function for score matching in diffusion models."""
    
    def __init__(self, reduce_mean: bool = True):
        """
        Initialize the score matching loss.
        
        Args:
            reduce_mean: Whether to average the loss over the batch
        """
        super().__init__()
        self.reduce_mean = reduce_mean
    
    def forward(
        self, 
        score_pred: torch.Tensor, 
        score_target: torch.Tensor, 
        mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Compute score matching loss.
        
        Args:
            score_pred: Predicted score of shape (batch_size, dim)
            score_target: Target score of shape (batch_size, dim)
            mask: Optional mask of shape (batch_size, dim)
            
        Returns:
            Loss value
        """
        # Compute squared error
        squared_error = (score_pred - score_target) ** 2
        
        # Apply mask if provided
        if mask is not None:
            squared_error = squared_error * mask
        
        # Sum over dimensions
        squared_error = squared_error.sum(dim=-1)
        
        # Average over batch if requested
        if self.reduce_mean:
            return squared_error.mean()
        else:
            return squared_error


class EMA:
    """Exponential Moving Average for model parameters."""
    
    def __init__(self, beta: float = 0.9999):
        """
        Initialize EMA.
        
        Args:
            beta: EMA decay rate
        """
        self.beta = beta
        self.step = 0
        self.shadow_params = {}
        self.backup_params = {}
    
    def register(self, model: nn.Module):
        """
        Register model parameters for EMA tracking.
        
        Args:
            model: PyTorch model
        """
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow_params[name] = param.data.clone()
    
    def update(self, model: nn.Module):
        """
        Update EMA parameters.
        
        Args:
            model: PyTorch model
        """
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow_params[name] = (
                    self.beta * self.shadow_params[name] + (1 - self.beta) * param.data
                )
    
    def apply_shadow(self, model: nn.Module):
        """
        Apply EMA parameters to model for inference.
        
        Args:
            model: PyTorch model
        """
        # Backup current parameters
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.backup_params[name] = param.data.clone()
                param.data = self.shadow_params[name]
    
    def restore(self, model: nn.Module):
        """
        Restore original parameters after inference.
        
        Args:
            model: PyTorch model
        """
        for name, param in model.named_parameters():
            if param.requires_grad and name in self.backup_params:
                param.data = self.backup_params[name]
        self.backup_params = {}


class DiffusionTrainer:
    """Trainer for diffusion models using score matching."""
    
    def __init__(
        self,
        model: nn.Module,
        diffusion_process: Any,
        optimizer: optim.Optimizer,
        scheduler: Optional[Any] = None,
        ema_decay: Optional[float] = 0.9999,
        device: torch.device = torch.device('cpu'),
        grad_clip_val: Optional[float] = None,
        checkpoint_dir: str = './checkpoints',
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize the diffusion trainer.
        
        Args:
            model: Score network model
            diffusion_process: Diffusion process
            optimizer: PyTorch optimizer
            scheduler: Learning rate scheduler
            ema_decay: EMA decay rate (if None, no EMA is used)
            device: Torch device
            grad_clip_val: Gradient clipping value
            checkpoint_dir: Directory for saving checkpoints
            logger: Logger
        """
        self.model = model
        self.diffusion = diffusion_process
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.device = device
        self.grad_clip_val = grad_clip_val
        self.checkpoint_dir = checkpoint_dir
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize EMA if requested
        self.ema = None
        if ema_decay is not None:
            self.ema = EMA(beta=ema_decay)
            self.ema.register(model)
        
        # Initialize loss function
        self.loss_fn = ScoreMatchingLoss()
        
        # Create checkpoint directory if it doesn't exist
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        
        # Track best validation loss
        self.best_val_loss = float('inf')
        self.epochs_without_improvement = 0
    
    def train_epoch(self, dataloader: DataLoader) -> float:
        """
        Train for one epoch.
        
        Args:
            dataloader: Training data loader
            
        Returns:
            Average training loss
        """
        self.model.train()
        total_loss = 0.0
        num_batches = len(dataloader)
        
        with tqdm(dataloader, desc="Training", leave=False) as pbar:
            for batch_idx, (data,) in enumerate(pbar):
                data = data.to(self.device)
                
                # Check if data has the expected shape
                original_shape = data.shape
                batch_size = data.shape[0]
                
                # Handle multi-dimensional data
                if len(data.shape) > 2:
                    print(f"Data has shape {data.shape}, standard diffusion models expect 2D [batch_size, dim]")
                    # Don't reshape here, let the diffusion process handle it
                
                # print(f"DEBUG train_epoch: data.shape={data.shape}")
                
                # Sample random time steps
                t = self.diffusion.sample_time(batch_size)
                # print(f"DEBUG train_epoch: t.shape={t.shape}")
                
                # Get noised samples and score targets
                # print(f"DEBUG train_epoch: before get_denoising_target")
                noised_data, score_target = self.diffusion.get_denoising_target(data, t)
                # print(f"DEBUG train_epoch: after get_denoising_target, noised_data.shape={noised_data.shape}, score_target.shape={score_target.shape}")
                
                # Calculate α_t and h_t
                # print(f"DEBUG train_epoch: before get_alpha_h")
                alpha_t, h_t = self.diffusion.get_alpha_h(t)
                
                # Reshape alpha_t and h_t for proper broadcasting
                if len(noised_data.shape) > 2:
                    reshape_dims = [batch_size] + [1] * (len(noised_data.shape) - 1)
                    alpha_t = alpha_t.view(*reshape_dims)
                    h_t = h_t.view(*reshape_dims)
                
                # print(f"DEBUG train_epoch: after get_alpha_h, alpha_t.shape={alpha_t.shape}, h_t.shape={h_t.shape}")

                # Compute model prediction
                score_pred = self.model(noised_data, t, alpha_t, h_t)
                
                # Calculate loss
                loss = self.loss_fn(score_pred, score_target)
                
                # Backpropagation
                self.optimizer.zero_grad()
                loss.backward()
                
                # Gradient clipping if specified
                if self.grad_clip_val is not None:
                    nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip_val)
                
                # Update parameters
                self.optimizer.step()
                
                # Update EMA if used
                if self.ema is not None:
                    self.ema.update(self.model)
                
                # Update metrics
                total_loss += loss.item()
                pbar.set_postfix({'loss': loss.item()})
            
            # Update learning rate
            if self.scheduler is not None:
                self.scheduler.step()
        
        # Restore original parameters if EMA was used
        if self.ema is not None:
            self.ema.restore(self.model)
        
        return total_loss / num_batches

    def validate(self, dataloader: DataLoader) -> float:
        """
        Validate the model.
        
        Args:
            dataloader: Validation data loader
            
        Returns:
            Average validation loss
        """
        self.model.eval()
        total_loss = 0.0
        num_batches = len(dataloader)
        
        # Use EMA parameters if available
        if self.ema is not None:
            self.ema.apply_shadow(self.model)
        
        with torch.no_grad():
            for batch_idx, (data,) in enumerate(dataloader):
                data = data.to(self.device)
                batch_size = data.shape[0]
                
                # Sample random time steps
                t = self.diffusion.sample_time(batch_size)
                
                # Get noised samples and score targets
                noised_data, score_target = self.diffusion.get_denoising_target(data, t)
                
                # Calculate α_t and h_t
                alpha_t, h_t = self.diffusion.get_alpha_h(t)
                
                # Compute model prediction
                score_pred = self.model(noised_data, t, alpha_t, h_t)
                
                # Calculate loss
                loss = self.loss_fn(score_pred, score_target)
                
                # Update metrics
                total_loss += loss.item()
        
        # Restore original parameters if EMA was used
        if self.ema is not None:
            self.ema.restore(self.model)
        
        return total_loss / num_batches

    def train(
        self, 
        train_loader: DataLoader, 
        val_loader: DataLoader, 
        num_epochs: int,
        early_stopping_patience: int = 10,
        save_best_only: bool = True,
        callback: Optional[Callable[[int, float, float], None]] = None
    ) -> Dict[str, List[float]]:
        """
        Train the model.
        
        Args:
            train_loader: Training data loader
            val_loader: Validation data loader
            num_epochs: Number of epochs to train
            early_stopping_patience: Number of epochs to wait for improvement before stopping
            save_best_only: Whether to save only the best model
            callback: Optional callback function called after each epoch
            
        Returns:
            Dictionary with training history
        """
        history = {
            'train_loss': [],
            'val_loss': [],
            'learning_rate': []
        }
        
        self.logger.info(f"Starting training for {num_epochs} epochs")
        start_time = time.time()
        
        for epoch in range(num_epochs):
            # Train for one epoch
            train_loss = self.train_epoch(train_loader)
            history['train_loss'].append(train_loss)
            
            # Validate
            val_loss = self.validate(val_loader)
            history['val_loss'].append(val_loss)
            
            # Get current learning rate
            current_lr = self.optimizer.param_groups[0]['lr']
            history['learning_rate'].append(current_lr)
            
            # Log progress
            self.logger.info(
                f"Epoch {epoch+1}/{num_epochs} - "
                f"Train Loss: {train_loss:.6f}, Val Loss: {val_loss:.6f}, "
                f"LR: {current_lr:.6f}"
            )
            
            # Check if this is the best model
            if val_loss < self.best_val_loss:
                self.best_val_loss = val_loss
                self.epochs_without_improvement = 0
                
                # Save the model
                if save_best_only:
                    self.save_checkpoint(f"best_model.pt")
                    self.logger.info(f"Saved best model with validation loss: {val_loss:.6f}")
            else:
                self.epochs_without_improvement += 1
                self.logger.info(f"No improvement for {self.epochs_without_improvement} epochs")
            
            # Save checkpoint for current epoch
            if not save_best_only:
                self.save_checkpoint(f"model_epoch_{epoch+1}.pt")
            
            # Call callback if provided
            if callback is not None:
                callback(epoch, train_loss, val_loss)
            
            # Check early stopping
            if self.epochs_without_improvement >= early_stopping_patience:
                self.logger.info(f"Early stopping after {epoch+1} epochs")
                break
        
        # Save final model if requested
        if not save_best_only:
            self.save_checkpoint("final_model.pt")
        
        elapsed_time = time.time() - start_time
        self.logger.info(f"Training completed in {elapsed_time:.2f} seconds")
        
        return history
    
    def save_checkpoint(self, filename: str):
        """
        Save model checkpoint.
        
        Args:
            filename: Checkpoint filename
        """
        checkpoint = {
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'best_val_loss': self.best_val_loss,
            'epochs_without_improvement': self.epochs_without_improvement
        }
        
        # Include EMA state if used
        if self.ema is not None:
            checkpoint['ema_shadow_params'] = self.ema.shadow_params
        
        # Include scheduler state if used
        if self.scheduler is not None:
            checkpoint['scheduler_state_dict'] = self.scheduler.state_dict()
        
        torch.save(checkpoint, os.path.join(self.checkpoint_dir, filename))
    
    def load_checkpoint(self, filepath: str, load_optimizer: bool = True) -> None:
        """
        Load model checkpoint.
        
        Args:
            filepath: Path to checkpoint file
            load_optimizer: Whether to load optimizer state
        """
        # Check if file exists
        if not os.path.isfile(filepath):
            self.logger.error(f"Checkpoint file {filepath} not found")
            return
        
        # Load checkpoint
        self.logger.info(f"Loading checkpoint from {filepath}")
        checkpoint = torch.load(filepath, map_location=self.device)
        
        # Load model state
        self.model.load_state_dict(checkpoint['model_state_dict'])
        
        # Load optimizer state if requested
        if load_optimizer and 'optimizer_state_dict' in checkpoint:
            self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        
        # Load EMA state if available
        if self.ema is not None and 'ema_shadow_params' in checkpoint:
            self.ema.shadow_params = checkpoint['ema_shadow_params']
        
        # Load scheduler state if available
        if self.scheduler is not None and 'scheduler_state_dict' in checkpoint:
            self.scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        
        # Load training state
        self.best_val_loss = checkpoint.get('best_val_loss', float('inf'))
        self.epochs_without_improvement = checkpoint.get('epochs_without_improvement', 0)
        
        self.logger.info(f"Checkpoint loaded successfully")


def create_trainer(
    model: nn.Module,
    diffusion_process: Any,
    config: Dict,
    device: torch.device
) -> DiffusionTrainer:
    """
    Create trainer with optimizer and scheduler.
    
    Args:
        model: Score network model
        diffusion_process: Diffusion process
        config: Training configuration
        device: Torch device
        
    Returns:
        DiffusionTrainer instance
    """
    # Create optimizer
    optimizer = optim.AdamW(
        model.parameters(),
        lr=config['learning_rate'],
        weight_decay=config['weight_decay']
    )
    
    # Create learning rate scheduler
    scheduler = optim.lr_scheduler.StepLR(
        optimizer,
        step_size=config['lr_scheduler_step'],
        gamma=config['lr_scheduler_gamma']
    )
    
    # Create trainer
    trainer = DiffusionTrainer(
        model=model,
        diffusion_process=diffusion_process,
        optimizer=optimizer,
        scheduler=scheduler,
        ema_decay=0.9999,  # Use EMA for better stability
        device=device,
        grad_clip_val=config['gradient_clip_val'],
        checkpoint_dir=config['checkpoint_dir']
    )
    
    return trainer

def train_diffusion_model(
    model: nn.Module,
    diffusion_process: Any,
    train_loader: DataLoader,
    val_loader: DataLoader,
    config: Dict,
    device: torch.device
) -> Tuple[nn.Module, Dict[str, List[float]]]:
    """
    Train diffusion model with score matching.
    
    Args:
        model: Score network model
        diffusion_process: Diffusion process
        train_loader: Training data loader
        val_loader: Validation data loader
        config: Training configuration
        device: Torch device
        
    Returns:
        Tuple of (trained model, training history)
    """
    # Create trainer
    trainer = create_trainer(model, diffusion_process, config, device)
    
    # Train model
    history = trainer.train(
        train_loader=train_loader,
        val_loader=val_loader,
        num_epochs=config['num_epochs'],
        early_stopping_patience=config['early_stopping_patience']
    )
    
    # Load best model
    best_model_path = os.path.join(config['checkpoint_dir'], "best_model.pt")
    if os.path.exists(best_model_path):
        trainer.load_checkpoint(best_model_path, load_optimizer=False)
    
    return model, history


if __name__ == "__main__":
    # Test the score matching trainer
    from config import TRAINING_CONFIG, DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, DEVICE
    from diffusion import DiffusionProcess
    from score_network import create_score_network
    from torch.utils.data import TensorDataset, DataLoader
    
    # Create dummy data
    asset_dim = 512
    factor_dim = 16
    batch_size = 32
    num_samples = 1000
    
    # Random data
    dummy_data = torch.randn(num_samples, asset_dim, device=DEVICE)
    train_data = dummy_data[:800]
    val_data = dummy_data[800:]
    
    # Create dataloaders
    train_dataset = TensorDataset(train_data)
    val_dataset = TensorDataset(val_data)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)
    
    # Create diffusion process
    diffusion = DiffusionProcess(
        T=DIFFUSION_CONFIG['T'],
        t0=DIFFUSION_CONFIG['t0'],
        eta=DIFFUSION_CONFIG['eta'],
        num_steps=DIFFUSION_CONFIG['num_steps'],
        device=DEVICE
    )
    
    # Create model
    model = create_score_network(SCORE_NETWORK_CONFIG, asset_dim, factor_dim, DEVICE)
    
    # Create trainer
    trainer = create_trainer(model, diffusion, TRAINING_CONFIG, DEVICE)
    
    # Test train epoch
    train_loss = trainer.train_epoch(train_loader)
    print(f"Train loss: {train_loss}")
    
    # Test validation
    val_loss = trainer.validate(val_loader)
    print(f"Validation loss: {val_loss}")
