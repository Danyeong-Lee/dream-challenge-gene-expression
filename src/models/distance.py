from typing import Any, List
import json
from collections import OrderedDict
import numpy as np
import pandas as pd

import torch
import torch.nn as nn
from pytorch_lightning import LightningModule
from torchmetrics import MaxMetric, PearsonCorrCoef, SpearmanCorrCoef
from cosine_annealing_warmup import CosineAnnealingWarmupRestarts


class DistanceNet(LightningModule):
    """Main default network"""
    """Use only forward strand"""
    def __init__(
        self,
        encoder: nn.Module,
        mlp: nn.Module,
        lr: float = 1e-3,
        weight_decay: float = 1e-5,
        lamb: float = 0.1
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["encoder", "mlp"])
        
        self.encoder = encoder
        self.mlp = mlp
        
        self.criterion = nn.MSELoss()
        
        self.dist = nn.CosineSimilarity()
        
        self.val_pearson = PearsonCorrCoef()
        self.test_pearson = PearsonCorrCoef()
        
        self.val_spearman = SpearmanCorrCoef()
        self.test_spearman = SpearmanCorrCoef()
        
        # for logging best so far validation accuracy
        self.val_spearman_best = MaxMetric()
        self.val_pearson_best = MaxMetric()
        
    def forward(self, fwd_x):
        h = self.encoder(fwd_x)
        h = self.mlp(h)
        
        return h.squeeze(-1)
    
    def on_train_start(self):
        # by default lightning executes validation step sanity checks before training starts,
        # so we need to make sure val_acc_best doesn't store accuracy from these checks
        self.val_spearman_best.reset()
        self.val_pearson_best.reset()
    
    def distance_step(self, batch):
        fwd_x, rev_x, y = batch
        
        fwd_h1 = self.encoder(fwd_x)
        rand_idx = torch.randperm(fwd_h1.size(0))
        
        fwd_h2 = fwd_h1[rand_idx]
        y2 = y[rand_idx]
        
        y_diff = torch.square(y - y2)
        y_diff = -y_diff
        emb_dist = self.dist(fwd_h1, fwd_h2)
        
        corr_loss = -torch.corrcoef(torch.cat([emb_dist.unsqueeze(0), y_diff.unsqueeze(0)], dim=0))[0][1]
        
        fwd_out = self.mlp(fwd_h1)
        
        preds = fwd_out.squeeze(-1)
        loss = self.criterion(preds, y)
        loss = loss + corr_loss * self.hparams.lamb
        
        return loss, preds, y
        
    def infer_step(self, batch):
        fwd_x, rev_x, y = batch
        preds = self(fwd_x)
        loss = self.criterion(preds, y)
        
        return loss, preds, y
    
    def training_step(self, batch, batch_idx):
        loss, preds, targets = self.distance_step(batch)
        metrics = {"train/loss_batch": loss}
        self.log_dict(metrics, on_step=False, on_epoch=True, prog_bar=True)
        
        return loss
        
    def validation_step(self, batch, batch_idx):
        loss, preds, targets = self.infer_step(batch)
        self.val_spearman.update(preds, targets)
        self.val_pearson.update(preds, targets)
        
        metrics = {"val/loss": loss}
        self.log_dict(metrics, on_step=False, on_epoch=True, prog_bar=True)
        
    def validation_epoch_end(self, outputs):
        # get val metric from current epoch
        epoch_spearman = self.val_spearman.compute()
        epoch_pearson = self.val_pearson.compute()
        
        if self.val_pearson.n_total.item() > 10000:
            # CV
            # log epoch metrics
            metrics = {"val/spearman": epoch_spearman, "val/pearson": epoch_pearson}
            self.log_dict(metrics, on_epoch=True, prog_bar=True)

            # log best metric
            self.val_spearman_best.update(epoch_spearman)
            self.val_pearson_best.update(epoch_pearson)
            self.log("val/spearman_best", self.val_spearman_best.compute(), on_epoch=True, prog_bar=True)
            self.log("val/pearson_best", self.val_pearson_best.compute(), on_epoch=True, prog_bar=True)
        else:
            # Validating with HQ_testata
            metrics = {"test/full_spearman": epoch_spearman, "test/full_pearson": epoch_pearson}
            self.log_dict(metrics, on_epoch=True, prog_bar=True)

            # log best metric
            self.val_spearman_best.update(epoch_spearman)
            self.val_pearson_best.update(epoch_pearson)
            self.log("test/full_spearman_best", self.val_spearman_best.compute(), on_epoch=True, prog_bar=True)
            self.log("test/full_pearson_best", self.val_pearson_best.compute(), on_epoch=True, prog_bar=True)
            
        # reset val metrics
        self.val_spearman.reset()
        self.val_pearson.reset()
        
    def test_step(self, batch, batch_idx):
        loss, preds, targets = self.infer_step(batch)
        self.test_spearman.update(preds, targets)
        self.test_pearson.update(preds, targets)
        metrics = {"test/loss": loss}
        self.log_dict(metrics, on_step=False, on_epoch=True, prog_bar=True)
        
    def test_epoch_end(self, outputs):
        # get val metric from current epoch
        epoch_spearman = self.test_spearman.compute()
        epoch_pearson = self.test_pearson.compute()
        
        # log epoch metrics
        metrics = {"test/spearman": epoch_spearman, "test/pearson": epoch_pearson}
        self.log_dict(metrics, on_epoch=True, prog_bar=True)
        
        
    def predict_step(self, batch, batch_idx):
        _, preds, _ = self.infer_step(batch)
        
        return preds
    
    def on_predict_epoch_end(self, outputs):
        with open("../../../../../sample_submission.json", "r") as f:
            ground = json.load(f)
    
        indices = np.array([int(indice) for indice in list(ground.keys())])
        PRED_DATA = OrderedDict()
        Y_pred = np.array(torch.cat(outputs[0]))

        for i in indices:
            PRED_DATA[str(i)] = float(Y_pred[i])
        
        
        with open("../../../../../submission.json", "w") as f:
            json.dump(PRED_DATA, f)

        print("Saved submission file!")
        
    def on_fit_end(self):
        self.trainer.save_checkpoint("last-swa.ckpt")
    
    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), 
                                lr=self.hparams.lr, 
                                weight_decay=self.hparams.weight_decay)


class DistanceNet_CA(DistanceNet):
    def __init__(
        self,
        encoder: nn.Module,
        mlp: nn.Module,
        lr: float = 1e-3,
        weight_decay: float = 1e-5,
        lamb: float = 0.1,
        max_epochs: int = 20,
        eta_min: float = 0.0
    ):
        super().__init__(encoder, mlp, lr, weight_decay, lamb)
        self.max_epochs = max_epochs
        self.eta_min = eta_min
    
    def configure_optimizers(self):
        n_steps = len(self.trainer._data_connector._train_dataloader_source.dataloader())
        
        optimizer = torch.optim.Adam(
            self.parameters(), 
            lr=self.hparams.lr, 
            weight_decay=self.hparams.weight_decay
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=self.max_epochs * n_steps,
            eta_min=self.eta_min
        )
        
        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
    
    
class DistanceNet_AW_CA(DistanceNet):
    def __init__(
        self,
        encoder: nn.Module,
        mlp: nn.Module,
        lr: float = 1e-4,
        weight_decay: float = 0,
        lamb: float = 0.1,
        max_epochs: int = 20,
        eta_min: float = 0.0
    ):
        super().__init__(encoder, mlp, lr, weight_decay, lamb)
        self.max_epochs = max_epochs
        self.eta_min = eta_min
    
    def configure_optimizers(self):
        n_steps = len(self.trainer._data_connector._train_dataloader_source.dataloader())
        
        optimizer = torch.optim.AdamW(
            self.parameters(), 
            lr=self.hparams.lr, 
            weight_decay=self.hparams.weight_decay
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=self.max_epochs * n_steps,
            eta_min=self.eta_min
        )
        
        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
    
    
