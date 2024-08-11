import torch
import math
import tiktoken
import time
import os
from torch.nn import functional as F
import torch.nn as nn
from dataclasses import dataclass

@dataclass
class GPTconfig:
    block_size: int = 1024
    vocab_size: int = 50257
    n_layers: int = 12

    n_head: int = 12
    n_embed: int = 768

class CasualSelfAttention(nn.Module):
    def __init__(self, config):
            super().__init__()
            assert config.n_embed % config.n_head == 0
            self.c_attn = nn.Linear(config.n_embed, 3*config.n_embed)
            self.c_proj = nn.Linear(config.n_embed, config.n_embed)

            # to control the growth of the activations we want to divide the activation by sqrt of num of features
            self.c_proj.FLAGGGG = 1
            self.n_head = config.n_head
            self.n_embed = config.n_embed
            self.register_buffer(
                "bias", torch.tril(torch.ones(config.block_size, config.block_size).view(1,1, config.block_size, config.block_size))
            )
    def forward(self, x):
        B, T, C = x.size()
        # print("B, T, C", B, T, C)
        # batch, sequence length, embedding dimesnion
        qkv = self.c_attn(x)
        # print(qkv.shape)
        q, k ,v = qkv.split(self.n_embed, dim=2)
        # print(k.shape)
        k = k.view(B, T, self.n_head, C//self.n_head).transpose(1,2)
        q = q.view(B, T, self.n_head, C//self.n_head).transpose(1,2)
        v = v.view(B, T, self.n_head, C//self.n_head).transpose(1,2)

        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:,:,:T,:T]==0, float('-inf'))
        att = F.softmax(att, dim=-1)
        y = att@v # basically weighted sum of interesting tokens!!
        y = y.transpose(1,2).contiguous().view(B, T, C)
        y = self.c_proj(y)
        
        return y
    

class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embed, 4 * config.n_embed, )
        self.gelu = nn.GELU(approximate='tanh')
        self.c_proj = nn.Linear(4*config.n_embed, config.n_embed)
        self.c_proj.FLAGGGG = 1
    def forward(self, x):
        x= self.c_proj(self.gelu(self.c_fc(x)))
        return x

class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.ln_1 = nn.LayerNorm(config.n_embed)
        self.attn = CasualSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embed)
        self.mlp = MLP(config)
        # attention is a aggregation function, its the information exchange
        # the attention is a communication operation, all the tokens coommm and exhange information
        # its a reduce operation, and MLP is a map operation. this is the map-reduce application!!!!

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

class GPT(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        #wte is weights of token embedding
        #wpe is weights of position embedding
        self.transformer = nn.ModuleDict(
            dict(
                wte = nn.Embedding(config.vocab_size, config.n_embed),
                wpe = nn.Embedding(config.block_size, config.n_embed),
                h = nn.ModuleList(Block(config) for _ in range(config.n_layers)),
                ln_f = nn.LayerNorm(config.n_embed) #gtp2 has this layer norm, original transformer does'nt have this.
            )
        )
        self.lm_head = nn.Linear(config.n_embed, config.vocab_size, bias=False)

        #weight sharing
        self.transformer.wte.weight = self.lm_head.weight

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            std = 0.02
            if hasattr(module, 'FLAGGGG'):
                std += (2*self.config.n_layer)**-0.5
            torch.nn.init.normal_(module.weight, mean=0.0, std = std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std = std)

    def forward(self, idx, targets = None):
        B, T = idx.size()
        # print(idx.device)
        assert T <= self.config.block_size 

        pos = torch.arange(0, T, dtype = torch.long, device = idx.device)
        pos_embedding = self.transformer.wpe(pos) # T, n_embed
        token_embedding = self.transformer.wte(idx) # B, T, n_embed
        x = token_embedding + pos_embedding # B,T, n_embed

        for block in self.transformer.h:
            x = block(x)

        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return logits, loss




    @classmethod
    def from_pretrained(cls, model_type):
        # loads gpt-2 weights from hugging face, cause i am gpu poor ;(
        from transformers import GPT2LMHeadModel
        assert model_type in {'gpt2'}
        print("pre trained gpt-2 weights: %s" % model_type)

        config_args = {
            'gpt2': dict(n_layers=12, n_head=12, n_embed=768) 
        }[model_type]
        config_args['vocab_size'] = 50257
        config_args['block_size'] = 1024

        #our model
        config = GPTconfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')]
        
        # with open('sd.txt','w') as file:
        #     for k in sd_keys:
        #         file.write(f"{k}: {sd[k].shape}\n")

        #hugging face model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')]
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')]
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # with open('sd_hf.txt','w') as file:
        #     for k in sd_keys_hf:
        #         file.write(f"{k}: {sd_hf[k].shape}\n")
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                # special treatment for the Conv1D weights we need to transpose
                # print("checking ", sd_hf[k].shape,sd[k].shape, sd_hf[k].shape[::-1] )
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    # print("workinggggggg")
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model

# generate
def generate():           
    num_return_sequences = 5
    max_length = 30
    model = GPT.from_pretrained('gpt2') 
    model.eval()
    model.to('cuda')
    print("hell yeah")
    enc = tiktoken.get_encoding('gpt2')
    tokens = enc.encode("Hello, i am under the water, ")
    tokens = torch.tensor(tokens, dtype=torch.long)
    tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1)
    x = tokens.to('cuda')
    torch.manual_seed(42)
    torch.cuda.manual_seed(42)
    while x.size(1) < max_length:
        with torch.no_grad():
            logits = model(x)
            logits = logits[:,-1,:] # B, vocab_size
            probs = F.softmax(logits, dim=-1)

            topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)

            ix = torch.multinomial(topk_probs, 1)
            xcol = torch.gather(topk_indices, -1, ix)
            x = torch.cat((x, xcol), dim=-1)

    for i in range(num_return_sequences):
        tokens = x[i, :max_length].tolist()
        decodeed = enc.decode(tokens)
        print(">", decodeed)
class DataLoader:
    def __init__(self, B, T):
        self.B = B
        self.T = T
        with open('input.txt','r') as f:
            text = f.read()
        enc = tiktoken.get_encoding('gpt2')
        tokens = enc.encode(text)
        self.tokens = torch.tensor(tokens)

        self.current_position = 0
    
    def next_batch(self):
        B, T = self.B, self.T
        buf = self.tokens[self.current_position : self.current_position + B*T+1]
        x = (buf[:-1]).view(B,T)
        y = (buf[1:]).view(B,T)
        self.current_position += B*T

        if self.current_position + (B*T+1) > len(self.tokens):
            self.current_position = 0

        return x, y

device = 'cuda'

train_loader = DataLoader(B = 16, T = 1024 )

model = GPT(GPTconfig()) # random initializaiton
model.to(device)

optimizer = torch.optim.AdamW(model.parameters(), lr = 3e-4)
for i in range(5):
    t0 = time.time()
    x,y  = train_loader.next_batch()
    x, y = x.to(device), y.to(device)

    optimizer.zero_grad()
   
    logits, loss = model(x,y)
    loss.backward()
    optimizer.step()
    torch.cuda.synchronize()
    t1= time.time()
    dt = (t1 - t0)*1000
    print(f"step {i}, loss: {loss.item()} time taken: {dt}ms")

print(loss)


