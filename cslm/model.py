import torch
import torchmetrics
from pytorch_lightning import LightningModule
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    get_linear_schedule_with_warmup,
    get_cosine_schedule_with_warmup,
    BertModel,
    AutoModel,
)
from torch.optim import AdamW
import torch.nn.functional as F
from pl_bolts.optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR

class LightningModel(LightningModule):
    def __init__(self, config):
        super().__init__()

        self.config = config
        self.save_hyperparameters()
        self.auto_config = AutoConfig.from_pretrained(self.config.upstream_model, num_labels=config.num_classes)
        
        self.model = AutoModelForSequenceClassification.from_pretrained(self.config.upstream_model, config=self.auto_config)         

        self.loss_fn = torch.nn.CrossEntropyLoss()
        if config.num_classes == 2:
            task = 'binary'
        else:
            task = 'multiclass'
        self.accuracy = torchmetrics.Accuracy(task=task, num_classes=config.num_classes)
        if config.freeze == 'true':
            # freeze the all weights except the classifier weights at the end
            for name, param in self.model.named_parameters():
                if 'classifier' not in name: # classifier layer
                    param.requires_grad = False

    def forward(self, input_ids, attention_mask):
        outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)        
        return outputs


    def training_step(self, batch, batch_idx):
        # batch
        input_ids = batch['input_ids']
        labels = batch['labels']
        attention_mask = batch['attention_mask']
        #token_type_ids = batch['token_type_ids']
        # fwd
        outputs = self(input_ids, attention_mask) # outputs are from a linear FC, NOT SOFTMAX
        # Tuple containing loss[in this case loss is not calculated], logits, hidden states and attentions,
        # since loss is None, only one element in tuple
        logits = outputs[0]
        preds = logits.view(-1, self.config.num_classes)
        pred_probs = F.softmax(preds, dim=1)
        # accuracy
        # float tensor of shape (N, C, ..), if preds is a floating point we apply torch.argmax along the C dimension 
        # to automatically convert probabilities/logits into an int tensor.
        acc = self.accuracy(preds, torch.argmax(labels, dim=1))
        # loss
        # torch.nn.CrossEntropyLoss() combines nn.LogSoftmax() and nn.NLLLoss() in one single class.
        # Therefore, you should not use softmax before.
        loss = self.loss_fn(preds, labels)

        return {'loss': loss, 'acc': acc} # For backprop

    def training_epoch_end(self, outputs):
        loss = torch.tensor([x['loss'] for x in outputs]).mean()
        train_acc = torch.tensor([x['acc'] for x in outputs]).mean()

        self.log('train/loss' , loss, on_step=False, on_epoch=True, prog_bar=True) # on_step = false, on_epoch = true, report average loss over the batch instead of per batch
        self.log('train/acc', train_acc, on_step=False, on_epoch=True, prog_bar=True)


    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']
        outputs = self(input_ids, attention_mask)

        labels = batch["labels"]
        logits = outputs[0]
        preds = logits.view(-1, self.config.num_classes)
        pred_probs = F.softmax(preds, dim=1)
        # accuracy
        acc = self.accuracy(preds, torch.argmax(labels, dim=1))
        # loss
        loss = self.loss_fn(preds, labels)

        return {"val_loss": loss, "val_acc": acc}

    def validation_epoch_end(self, outputs):
        val_acc = torch.tensor([x['val_acc'] for x in outputs]).mean()
        loss = torch.tensor([x['val_loss'] for x in outputs]).mean()
        self.log('val/loss' , loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log('val/acc',val_acc, on_step=False, on_epoch=True, prog_bar=True)

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']
        outputs = self(input_ids, attention_mask)

        labels = batch["labels"]
        logits = outputs[0]

        preds = logits.view(-1, self.config.num_classes)
        pred_probs = F.softmax(preds, dim=1)
        # accuracy
        acc = self.accuracy(preds, torch.argmax(labels, dim=1))
        # loss
        loss = self.loss_fn(preds, labels)

        return {"test_loss": loss, "test_acc": acc}

    def test_epoch_end(self, outputs):
        train_acc = torch.tensor([x['test_acc'] for x in outputs]).mean()
        loss = torch.tensor([x['test_loss'] for x in outputs]).mean()
        self.log('test/loss' , loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log('test/acc',train_acc, on_step=False, on_epoch=True, prog_bar=True)

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""
        adam_epsilon = 1e-8
        warmup_steps = 0
        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.config.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.config.lr, eps=adam_epsilon)

        #scheduler = get_linear_schedule_with_warmup(
        #    optimizer,
        #    num_warmup_steps=warmup_steps,
        #    num_training_steps=self.trainer.estimated_stepping_batches,
        #)
        #scheduler = LinearWarmupCosineAnnealingLR(optimizer, warmup_epochs=5, max_epochs=30)
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=5,
            num_training_steps=self.config.epochs,
        )
        scheduler = {"scheduler": scheduler, "interval": "step"}
        return [optimizer], [scheduler]