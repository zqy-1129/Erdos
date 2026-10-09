"""Benchmark actual CUDA inference before changing the pinned release model runtime."""
import os,sys,time
from pathlib import Path
from . import audit,core
audit.deps()
sys.path.insert(0,str(audit.RUNTIME/'py38-gpu119'))
old=Path.resolve
try:
    Path.resolve=lambda self,strict=False:self.absolute()
    import torch,onnxruntime as ort
finally:Path.resolve=old
handle=os.add_dll_directory(str(Path(torch.__file__).parent/'lib'))
from . import services
original=services.model
start=time.monotonic()
session=ort.InferenceSession(str(audit.RUNTIME/'embedding-model/model_optimized.onnx'),providers=[('CUDAExecutionProvider',{'gpu_mem_limit':2147483648,'arena_extend_strategy':'kSameAsRequested','cudnn_conv_algo_search':'DEFAULT'}),'CPUExecutionProvider'])
if session.get_providers()[0]!='CUDAExecutionProvider':raise ValueError('CUDA embedding unavailable')
from tokenizers import Tokenizer
tokenizer=Tokenizer.from_file(str(audit.RUNTIME/'embedding-model/tokenizer.json'))
tokenizer.enable_truncation(max_length=128);tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
services._model=(session,tokenizer)
texts=['建立模型进行资源优化调度，验证约束并检查求解精度。'*20]*64
before=time.monotonic();vectors=services.embed(texts);elapsed=time.monotonic()-before
report={'providers':session.get_providers(),'ort_version':ort.__version__,'initialization_seconds':before-start,'texts':len(texts),'inference_seconds':elapsed,'dimensions':len(vectors[0])}
core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_embedding_cuda_benchmark.json',report)
print(report,flush=True)
