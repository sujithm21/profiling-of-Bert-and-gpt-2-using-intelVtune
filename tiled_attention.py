import torch
import torch.nn as nn
from transformers import GPT2Model, GPT2Config, GPT2Tokenizer

# Optimized tiled matrix multiplication function
def tiled_matmul(A, B, tile_size):
    batch_size, num_heads, n, m = A.shape
    _, _, _, p = B.shape
    
    # Initialize the output matrix
    C = torch.zeros((batch_size, num_heads, n, p), device=A.device)
    
    # Iterate over tiles efficiently
    for i in range(0, n, tile_size):
        for j in range(0, p, tile_size):
            for k in range(0, m, tile_size):
                # Define the tile sub-matrices
                A_tile = A[:, :, i:i+tile_size, k:k+tile_size]
                B_tile = B[:, :, k:k+tile_size, j:j+tile_size]
                
                # Perform multiplication on tiles
                C[:, :, i:i+tile_size, j:j+tile_size] += torch.matmul(A_tile, B_tile)
                
    return C

# Define the custom TiledAttention layer
class TiledAttention(nn.Module):
    def __init__(self, hidden_size, num_attention_heads, tile_size):
        super(TiledAttention, self).__init__()
        self.hidden_size = hidden_size
        self.num_attention_heads = num_attention_heads
        self.tile_size = tile_size

        self.query = nn.Linear(hidden_size, hidden_size)
        self.key = nn.Linear(hidden_size, hidden_size)
        self.value = nn.Linear(hidden_size, hidden_size)
        self.out = nn.Linear(hidden_size, hidden_size)
        
        self.softmax = nn.Softmax(dim=-1)
        self.scale = 1.0 / (hidden_size // num_attention_heads) ** 0.5

    def forward(self, hidden_states):
        batch_size, seq_length, hidden_size = hidden_states.size()

        # Linear projections
        q = self.query(hidden_states)
        k = self.key(hidden_states)
        v = self.value(hidden_states)
        
        # Reshape for multi-head attention
        q = q.view(batch_size, seq_length, self.num_attention_heads, hidden_size // self.num_attention_heads).transpose(1, 2)
        k = k.view(batch_size, seq_length, self.num_attention_heads, hidden_size // self.num_attention_heads).transpose(1, 2)
        v = v.view(batch_size, seq_length, self.num_attention_heads, hidden_size // self.num_attention_heads).transpose(1, 2)
        
        # Scaled dot-product attention
        scores = tiled_matmul(q, k.transpose(-1, -2), self.tile_size) * self.scale
        attention_weights = self.softmax(scores)
        attention_output = tiled_matmul(attention_weights, v, self.tile_size)
        
        # Concatenate heads
        attention_output = attention_output.transpose(1, 2).contiguous().view(batch_size, seq_length, hidden_size)
        
        # Final linear layer
        output = self.out(attention_output)
        
        return output

# Extend the GPT-2 model to replace the attention layers with TiledAttention
class GPT2WithTiledAttention(GPT2Model):
    def __init__(self, config, tile_size):
        super(GPT2WithTiledAttention, self).__init__(config)
        self.tile_size = tile_size

        # Replace the attention layers with TiledAttention
        for block in self.h:
            block.attn.c_attn = nn.Linear(config.n_embd, config.n_embd * 3)
            block.attn.c_proj = nn.Linear(config.n_embd, config.n_embd)
            block.attn.split_size = config.n_embd

            # Replace the attention mechanism with TiledAttention
            block.attn.attn = TiledAttention(config.n_embd, config.n_head, tile_size)

# Initialize the GPT-2 configuration and model with tiled attention
config = GPT2Config()
tile_size = 64
model_tiled = GPT2WithTiledAttention(config, tile_size)

# Initialize the tokenizer
tokenizer = GPT2Tokenizer.from_pretrained('gpt2')

# Sample sentence
sentence = "Hello, how are you?"
input_ids = tokenizer.encode(sentence, return_tensors='pt')

# Move model to GPU if available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model_tiled.to(device)
input_ids = input_ids.to(device)

# Define a function to profile attention layers
def profile_attention(model, inputs, model_type="tiled"):
    def attention_hook(module, input, output):
        return output
    
    handles = []
    # Register hooks on attention layers
    for layer in model.h:
        handle = layer.attn.register_forward_hook(attention_hook)
        handles.append(handle)
    
    # Perform forward pass with profiling
    with torch.autograd.profiler.profile() as prof:
        outputs = model(inputs)
    
    # Remove hooks
    for handle in handles:
        handle.remove()

    # Save profiling results
    prof.export_chrome_trace(f"profiler_{model_type}.json")

    # Print the profiling results
    print(f"\n{model_type.capitalize()} Attention Layer Profiling:")
    print(prof.key_averages().table(sort_by="cpu_time_total", row_limit=10))

# Profile the attention layers
profile_attention(model_tiled, input_ids, "tiled")

# Run the model with tiled attention
with torch.no_grad():
    output_tiled = model_tiled(input_ids)

print(f"Output shape (tiled): {output_tiled.last_hidden_state.shape}")
