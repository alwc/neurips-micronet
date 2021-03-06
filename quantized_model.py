try:
    import distiller
except ImportError:
    pass
from u import * # util functions
from data import * # data loader

distiller_vs_explicit = 'distiller'

class ExplicitQuantize(nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('max_abs', torch.tensor(1.0))
        self.register_buffer('inv_scale', torch.tensor(1.0))

    def forward(self, x):
        x = x.clamp(min=-self.max_abs, max=self.max_abs)
        return torch.round(x / self.inv_scale) * self.inv_scale
        
class ExplicitReLUQuantize(ExplicitQuantize):
    def forward(self, x):
        # We can treat this as a fake fused quantized relu
        return super(ExplicitReLUQuantize, self).forward(x.relu())

def Quantize():
    if distiller_vs_explicit == 'distiller':
        # Distiller will handle this during training and wrap it with range_linear.FakeQuantizationWrapper
        return nn.Identity()
    elif distiller_vs_explicit == 'explicit':
        # At evaluation time, we explicitly perform the fake quantization in our code so it's easily verifiable
        return ExplicitQuantize()

def ReLUQuantize():
    if distiller_vs_explicit == 'distiller':
        return nn.ReLU()
    elif distiller_vs_explicit == 'explicit':
        return ExplicitReLUQuantize()

class AdaptiveEmbedding(nn.Module):
    def __init__(self, c):
        super(AdaptiveEmbedding, self).__init__()
        self.c = c

        n_embeds = c.n_embeds
        assert n_embeds[0] == c.n_embed

        self.layers = nn.ModuleList(
            nn.Embedding(end - start, n_embed_i) for n_embed_i, start, end in zip(
                n_embeds, [0] + c.cutoffs, c.cutoffs + [c.n_vocab]
            )
        )
        self.projections = nn.ModuleList(
            nn.Linear(n_embed_i, c.n_embed, bias=False) for n_embed_i in n_embeds[1:]
        )
        for layer in self.layers:
            nn.init.normal_(layer.weight, 0, 0.02)
        
    def forward(self, x):
        c = self.c
        x_flat = x.reshape(-1)

        emb_flat = torch.zeros([x_flat.size(0), c.n_embed], dtype=torch.float, device=x.device)
        for i, (layer, start, end) in enumerate(zip(self.layers, [0] + c.cutoffs, c.cutoffs + [c.n_vocab])):
            mask_i = (x_flat >= start) & (x_flat < end)
            indices_i = mask_i.nonzero().squeeze()

            if indices_i.numel() == 0:
                continue

            inp_i = x_flat.index_select(0, indices_i) - start
            emb_i = layer(inp_i)
            if i > 0:
                emb_i = self.projections[i - 1](emb_i)
            emb_flat.index_copy_(0, indices_i, emb_i)

        emb = emb_flat.view(*x.size(), c.n_embed)
        emb.mul_(np.sqrt(c.n_embed))
        return emb

class ProjectedAdaptiveLogSoftmax(nn.Module):
    def __init__(self, c):
        super(ProjectedAdaptiveLogSoftmax, self).__init__()
        self.c = c
        n_layers = len(c.cutoffs) + 1

        n_embeds = c.n_embeds
        assert n_embeds[0] == c.n_embed
        assert n_layers == len(n_embeds)

        # the first layer gets (n_layers - 1) more classes to determine if token belongs in those layers
        self.clusters = nn.Linear(c.n_embed, n_layers - 1)
        self.layers = nn.ModuleList(
            nn.Linear(n_embed_i, end - start) for n_embed_i, start, end in zip(
                n_embeds, [0] + c.cutoffs, c.cutoffs + [c.n_vocab]
            )
        )
        self.projections = nn.ModuleList(
            nn.Linear(c.n_embed, n_embed_i, bias=False) for n_embed_i in n_embeds[1:]
        )
        self.cache_keys = self.cache_values = None

        theta = torch.tensor(c.cache_theta_init)
        self.cache_theta_inv_softplus = nn.Parameter((theta.exp() - 1).log())
        
        lam = torch.tensor(c.cache_lambda_init)
        self.cache_lambda_inv_sigmoid = nn.Parameter((lam / (1 - lam)).log())

    def query_cache(self, hidden, target):
        # assume n_batch = 1
        c = self.c

        n_seq = hidden.size(0)
        cache_keys, cache_values = self.cache_keys, self.cache_values
        if cache_keys is None:
            n_cache = 0
            cache_keys = hidden
            cache_values = target
        else:
            n_cache = cache_keys.size(0)
            cache_keys = torch.cat((cache_keys.detach(), hidden))
            cache_values = torch.cat((cache_values, target))
        cache_keys = cache_keys[-(c.n_cache + n_seq):]
        cache_values = cache_values[-(c.n_cache + n_seq):]
        
        self.last_theta = theta = F.softplus(self.cache_theta_inv_softplus)
        attn = theta * F.pad(hidden.mm(cache_keys.t()), (c.n_cache - n_cache, 0), value=-1e8) # (n_s, n_c + n_s)
        # (n_s, n_cache)
        logprobs = attn.reshape(-1).unfold(0, c.n_cache, attn.size(1) + 1).log_softmax(dim=1)
        indices = F.pad(cache_values, (c.n_cache - n_cache, 0), value=-1).unfold(0, c.n_cache, 1)[:n_seq]

        mask = indices != target[:, None]
        logprobs = logprobs - mask.to(logprobs.dtype) * 10e8
        logprobs[torch.isnan(logprobs)] = -np.inf

        pos_logprob = logprobs.logsumexp(dim=1)
        self.cache_keys = cache_keys[-c.n_cache:]
        self.cache_values = cache_values[-c.n_cache:]
        return pos_logprob
        
    def forward(self, hidden, target):
        # hidden: (n_seq * n_batch, n_embed)
        # target: (n_seq * n_batch)
        c = self.c
        assert hidden.size(0) == target.size(0), 'Input and target should have the same size in the batch dimension'
        
        cache_logprob = self.query_cache(hidden, target)

        head_logit = torch.cat((self.layers[0](hidden), self.clusters(hidden)), dim=1)

        head_logprob = head_logit.log_softmax(dim=1)

        nll = torch.zeros_like(target, dtype=hidden.dtype, device=hidden.device)

        offset = 0
        for i, (start, end) in enumerate(zip([0] + c.cutoffs, c.cutoffs + [c.n_vocab])):
            mask_i = (target >= start) & (target < end)
            
            indices_i = mask_i.nonzero().squeeze()

            if indices_i.numel() == 0:
                continue
            
            cache_logprob_i = cache_logprob.index_select(0, indices_i)
            
            target_i = (target.index_select(0, indices_i) - start)[:, None]
            head_logprob_i = head_logprob.index_select(0, indices_i)

            if i == 0:
                logprob_i = head_logprob_i.gather(1, target_i).squeeze(1)
            else:
                hidden_i = hidden.index_select(0, indices_i)
                proj_i = self.projections[i - 1](hidden_i)
                tail_logit_i = self.layers[i](proj_i)
                tail_logprob_i = tail_logit_i.log_softmax(dim=1)
                
                logprob_i = head_logprob_i[:, -i] + tail_logprob_i.gather(1, target_i).squeeze(1)
            
            self.last_lambda = cache_lambda = self.cache_lambda_inv_sigmoid.sigmoid()
            logprob_i = torch.stack([cache_lambda.log() + cache_logprob_i, (1 - cache_lambda).log() + logprob_i]).logsumexp(dim=0)
            
            nll[offset: offset + logprob_i.size(0)].copy_(-logprob_i)
            offset += logprob_i.size(0)

        return nll

class Decoder(nn.Module):
    def __init__(self, c, layer_i):
        super(Decoder, self).__init__()
        self.layer_i = layer_i
        n_embed = c.n_embed
       
        self.ln1 = nn.LayerNorm(n_embed)
        self.quant_ln1 = Quantize()

        self.qkv = nn.Linear(n_embed, c.n_head * (2 * c.n_k + c.n_v))
        self.quant_qkv = Quantize()

        self.pos_emb = nn.Parameter(torch.Tensor(c.n_k, c.n_seq + 1))
        nn.init.normal_(self.pos_emb, 0, 0.02)

        self.quant_attn = Quantize()
        self.quant_attnv = Quantize()

        self.out = nn.Linear(c.n_head * c.n_v, n_embed, bias=False)
        self.dropout = nn.Dropout(c.dropout)

        self.ln2 = nn.LayerNorm(c.n_embed)
        self.quant_ln2 = Quantize()

        self.fc = nn.Sequential(
            nn.Linear(c.n_embed, c.n_inner),
            ReLUQuantize(),
            nn.Dropout(c.dropout),
            nn.Linear(c.n_inner, c.n_embed),
            Quantize(),
            nn.Dropout(c.dropout),
        )
        self.c = c

    def forward(self, x, prev=None):
        # x: (n_group * n_seq, n_batch, n_embed)
        # pos_emb: (n_k, n_seq + 1)
        # mask: (2 * n_seq, 2 * n_seq) parallelogram
        
        c = self.c
        n_s = c.n_seq
        n_g = x.size(0) // n_s
        n_b = x.size(1)
        n_h = c.n_head
        n_bh = n_b * n_h
        n_k = c.n_k
        n_v = c.n_v
        
        qkv = self.quant_qkv(self.qkv(
            self.quant_ln1(self.ln1(x))
        )).reshape(n_g * n_s, n_b * n_h, 2 * n_k + n_v)
        q, kv = qkv.split([n_k, n_k + n_v], dim=-1)
        
        q = q.reshape(n_g, n_s, n_b * n_h, n_k)

        padding = prev if prev is not None else torch.zeros((n_s, n_b * n_h, n_k + n_v), dtype=kv.dtype, device=kv.device)
        kv = torch.cat((padding, kv))
        k, v = kv.unfold(0, 2 * n_s, n_s).split([n_k, n_v], dim=2) # (n_g, n_bh, n_kv, 2 * n_s)

        qk = torch.bmm(
            q.transpose(1, 2).reshape(n_g * n_bh, n_s, n_k),
            k.reshape(n_g * n_bh, n_k, 2 * n_s)
        ).reshape(n_g, n_bh, n_s * 2 * n_s).unfold(2, n_s + 1, 2 * n_s + 1) # (n_g, n_bh, n_s, n_s + 1)

        qe = torch.matmul(q, self.pos_emb).transpose(1, 2)

        attn = qk + qe
        attn.mul_(n_k ** -0.5)
        
        attn = attn.softmax(dim=-1)
        attn = self.quant_attn(attn)

        attn = F.pad(attn, (0, n_s))
        attn = attn.reshape(n_g, n_b * n_h, -1).unfold(2, 2 * n_s, 2 * n_s) # (n_g, n_bh, n_s, 2 * n_s)

        attnv = torch.bmm(
            attn.reshape(n_g * n_bh, n_s, 2 * n_s),
            v.transpose(2, 3).reshape(n_g * n_bh, 2 * n_s, n_v)
        ).reshape(n_g, n_bh, n_s, n_v).transpose(1, 2)
        attnv = self.quant_attnv(attnv)

        attn_out = self.out(attnv.reshape(n_g * n_s, n_b, n_h * n_v)) # (n_g * n_s, n_b, n_embed)
        attn_out = self.dropout(attn_out)

        in_attn = x + attn_out
        
        out = in_attn + self.fc(self.quant_ln2(self.ln2(in_attn)))
        next = kv[-n_s:].detach()
        return out, next

class Transformer(nn.Module):
    def __init__(self, c):
        super(Transformer, self).__init__()
        self.c = c
        self.embed = AdaptiveEmbedding(c)

        self.dropout1 = nn.Dropout(c.dropout)

        self.layers = nn.ModuleList(Decoder(c, i) for i in range(c.n_layers))
        
        self.quant = Quantize()
        self.dropout2 = nn.Dropout(c.dropout)
        
        self.loss = ProjectedAdaptiveLogSoftmax(c)

        # tie output embedding weights to input embedding weights
        for layer_embed, layer_loss in zip(self.embed.layers, self.loss.layers):
            layer_loss.weight = layer_embed.weight

    def forward(self, inputs, labels, prevs=None):
        # inputs: (n_group * n_seq, n_batch)
        # labels: (n_group * n_seq, n_batch)
        c = self.c

        n_gs = inputs.size(0)
        n_s = c.n_seq
        if n_gs % n_s != 0:
            padding = torch.zeros((n_s - n_gs % n_s, inputs.size(1)), dtype=inputs.dtype, device=inputs.device)
            inputs = torch.cat((inputs, padding))
            labels = torch.cat((labels, padding))

        x = self.embed(inputs)
        x = self.dropout1(x)

        prevs = prevs or [None] * c.n_layers
        nexts = []
        for layer, prev in zip(self.layers, prevs):
            x, prev = layer(x, prev=prev)
            nexts.append(prev)
        
        x = self.quant(x)
        x = self.dropout2(x)

        loss = self.loss(x.reshape(-1, x.size(2)), labels.reshape(-1))

        loss = loss.reshape(labels.shape)[:n_gs].mean() # this mean is okay because it averages over the sequence (rather than average within a single token)
        return loss, nexts, self.loss.last_lambda, self.loss.last_theta

get_net = Transformer
get_opt = lambda c, net: optim.Adam(net.parameters(), lr=c.lr)

def scheduler(c, opt, step):
    c.setdefault(
        scheduler=None,
        step_warmup=0,
    )
    get_lr = lambda step: c.lr * min(1, max(step, 1) / max(1, c.step_warmup))
    def step_lr(step):
        lr = get_lr(step)
        for g in opt.param_groups:
            g['lr'] = float(lr)
    if c.scheduler == 'cosine':
        scheduler = optim.lr_scheduler.CosineAnnealingLR(opt, c.steps, last_epoch=step or step - 1)
        warmup_step_lr = step_lr
        def step_lr(step):
            if step <= c.step_warmup:
                warmup_step_lr(step)
            else:
                scheduler.step(step)
    elif c.scheduler == 'rsqrt':
        warmup = get_lr
        get_lr = lambda step: warmup(step) / np.sqrt(max(1, c.step_warmup, step))
    return step_lr

def train(c):
    net = get_net(c)

    opt = get_opt(c, net)
    net, opt, step = c.init_model(net, opt=opt, step='max', train=True)

    step_lr = scheduler(c, opt, step)
    data_tr = SampleIterator(c, c.train_batch, split='valid' if c.debug else 'train')
    iter_tr = iter(data_tr)
    data_val = SequentialIterator(c, c.eval_batch, split='valid')
    data_test = SequentialIterator(c, c.eval_batch, split='test')

    print('Before quantization')
    tbl, sparsity = distiller.weights_sparsity_tbl_summary(net, return_total_sparsity=True)
    step_result = pd.Series(evaluate(c, data_val, net)).add_prefix('val_')
    step_result = step_result.append(
        pd.Series(evaluate(c, data_test, net)).add_prefix('test_')
    )
    step_result['sparsity'] = sparsity
    print(step_result)

    compression_scheduler = distiller.config.file_config(net, opt, c.compress)

    print('After initial quantization')
    s = Namespace(net=net, opt=opt, step=step)
    c.on_train_start(s)

    tbl, sparsity = distiller.weights_sparsity_tbl_summary(net, return_total_sparsity=True)
    step_result = pd.Series(evaluate(c, data_val, net)).add_prefix('val_')
    step_result = step_result.append(
        pd.Series(evaluate(c, data_test, net)).add_prefix('test_')
    )
    step_result['sparsity'] = sparsity
    print(step_result)

    npm = []    
    for name, param in net.named_parameters():
        if param.dim() in [2, 4] and any(type in name for type in ['weight', 'bias']):
            npm.append((name, param, param.abs() == 0))

    best_val_loss = np.inf
    if s.results is not None and 'val_loss' in s.results.columns:
        best_val_loss = s.results['val_loss'].dropna().max()
    try:
        steps_per_epoch = c.step_eval
        while step < s.step_max:
            epoch = step // steps_per_epoch
            batch = step % steps_per_epoch

            if batch == 0:
                compression_scheduler.on_epoch_begin(epoch)
            compression_scheduler.on_minibatch_begin(epoch, batch, steps_per_epoch)
            
            step_lr(step)

            x = to_torch(next(iter_tr), c.device).t()

            t_s = time()
            inputs, labels = x[:-1], x[1:]
            loss, _, lam, theta = net(inputs, labels)
            
            compression_scheduler.before_backward_pass(epoch, batch, steps_per_epoch, loss, False)

            opt.zero_grad()
            if torch.isnan(loss):
                import q; q.d()
            
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), c.get('clip_grad', 0.5))

            compression_scheduler.before_parameter_optimization(epoch, batch, steps_per_epoch, opt)
            opt.step()
            for name, param, mask in npm:
                param.data[mask] = 0
            compression_scheduler.on_minibatch_end(epoch, batch, steps_per_epoch)
            
            if (batch + 1) == steps_per_epoch:
                compression_scheduler.on_epoch_end(epoch)

            time_model = np.round(time() - t_s, 5)

            loss = from_torch(loss)
            perplexity = np.nan if loss > 5 else np.e ** loss
            step_result = pd.Series(dict(
                loss=loss,
                perplexity=perplexity,
                time=time_model,
            )).add_prefix('train_')
            step_result['lr'] = next(iter(opt.param_groups))['lr']
            step_result['theta'] = from_torch(theta)
            step_result['lambda'] = from_torch(lam)

            s.step = step = step + 1
            if step % c.step_eval == 0:
                tbl, sparsity = distiller.weights_sparsity_tbl_summary(net, return_total_sparsity=True)
                step_result = step_result.append(
                    pd.Series(evaluate(c, data_val, net)).add_prefix('val_')
                )
                step_result = step_result.append(
                    pd.Series(evaluate(c, data_test, net)).add_prefix('test_')
                )
                step_result['sparsity'] = sparsity
                s.record_step = step_result['val_loss'] < best_val_loss
                clear_gpu_memory()
            s.step_result = step_result
            c.on_step_end(s)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        if c.main:
            c.log(err)
        else:
            print(err)
    finally:
        c.on_train_end(s)

def evaluate(c, data, net):
    clear_gpu_memory()
    was_training = net.training
    net.eval()
    
    t_s = time()
    with torch.no_grad():
        weights = []
        losses = []
        prevs = None

        for batch in data:
            x = to_torch(batch, c.device).t()
            inputs, labels = x[:-1], x[1:]

            loss, prevs, _, _ = net.forward(inputs, labels, prevs=prevs)
            losses.append(loss)
            weights.append(labels.size(0))
        weights = np.array(weights)
        weights = weights / weights.sum()
        loss = sum(x * w for x, w in zip(losses, weights))

    if was_training:
        net.train()
    loss = from_torch(loss)
    perplexity = np.nan if loss > 5 else np.e ** loss
    return dict(loss=loss, perplexity=perplexity, time=np.round(time() - t_s))

if __name__ == '__main__':
    c = Config.from_args()
    train(c)