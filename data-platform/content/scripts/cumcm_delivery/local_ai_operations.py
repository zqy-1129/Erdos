"""Install service operations outside the project; contains no model implementation."""
import hashlib,json
from pathlib import Path
from . import core,audit,local_closure

START=r'''$ErrorActionPreference = 'Stop'
$aiDirectory = $PSScriptRoot
$pythonExecutable = 'E:\Anaconda\envs\pytorch\python.exe'
$serviceSpecs = @(
 @{Script='service.py'; Port=18790; Log='service'},
 @{Script='formula_service_v3.py'; Port=18794; Log='formula-v3'}
)
foreach ($spec in $serviceSpecs) {
 $scriptPath = Join-Path $aiDirectory $spec.Script
 $existing = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -like 'python*' -and $_.CommandLine -like ('*' + $scriptPath + '*')
 }
 if (-not $existing) {
  Start-Process -FilePath $pythonExecutable -ArgumentList @('-X','utf8',$scriptPath) `
   -WorkingDirectory $aiDirectory -WindowStyle Hidden `
   -RedirectStandardOutput (Join-Path $aiDirectory ($spec.Log+'.stdout.log')) `
   -RedirectStandardError (Join-Path $aiDirectory ($spec.Log+'.stderr.log')) | Out-Null
 }
 $ready = $false
 for ($attempt=0; $attempt -lt 60; $attempt++) {
  try {
   $health = Invoke-RestMethod ('http://127.0.0.1:'+$spec.Port+'/health') -TimeoutSec 2
   if ($health.status -eq 'ready') { $ready=$true; break }
  } catch { Start-Sleep -Milliseconds 500 }
 }
 if (-not $ready) { throw ('Local API failed to become ready: '+$spec.Script) }
 Write-Output ('Local API ready: 127.0.0.1:'+$spec.Port+'; credentials omitted')
}
Copy-Item -LiteralPath (Join-Path $aiDirectory 'formula-api-v3.json') `
 -Destination (Join-Path $aiDirectory 'formula-api.json') -Force
'''
STOP=r'''$ErrorActionPreference = 'Stop'
foreach ($name in @('service.py','formula_service_v3.py')) {
 $scriptPath = Join-Path $PSScriptRoot $name
 $owned = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -like 'python*' -and $_.CommandLine -like ('*'+$scriptPath+'*')
 }
 foreach ($process in $owned) { Stop-Process -Id $process.ProcessId }
}
Write-Output 'Stopped matching local AI service processes; files preserved.'
'''
README='''# Erdos 本地模型 API 运维

本目录在工程目录外。服务只绑定 127.0.0.1，历史资料不发送第三方模型。
解释器固定 E:\\Anaconda\\envs\\pytorch\\python.exe。MAMBA 未参与本流水线。

- 启动：PowerShell 执行 ./start_local_ai.ps1。
- 停止：./stop_local_ai.ps1，只停止明确匹配服务脚本路径的进程。
- 文本/视觉服务：18790，POST /v1/infer；Qwen2-VL-2B-Instruct 的固定公开版本。
- 公式服务：18794，POST /v1/formulas；固定 Pix2Text MFD/MFR ONNX。
- detect_only=true 返回区域；transcribe_only=true 直接读取已校验公式截图。
- 公式检测使用有显存限额的 GPU，转写使用 CPU。先前全 GPU 试验服务不作为运行依赖。
- GET /health 只给就绪状态和版本；不返回凭据。

私有令牌位于 api.json 和 formula-api.json，由调用方读取，不放入 exe、日志或交付报告。
不要把本目录作为客户端发行资源。源码和依赖、权重分别在 service*.py、deps/、deps-gpu/、models/。
模型源版本、权重与服务脚本的 SHA256 见 model_lock.json。权重的 LFS SHA 已与公开模型仓库元数据核对。
HF_HUB_OFFLINE 与 TRANSFORMERS_OFFLINE 已启用，运行不自动下载或向云端上传。

识别与写作输出都是机器候选，不能据其概率宣布数学语义正确。使用原页复核；新题结果只来自真实计算。
小型视觉模型在部分页面会输出坐标或错误标题，质量拒用记录保存在数据工程交付中。
运行日志不记录输入正文、图片或令牌。本目录需按团队内部数据设置访问权限。
'''

def install():
    root=Path('D:/Erdos_LocalAI');rows=[];pins={}
    for name in ('qwen','mfd','mfr'):pins[name]=json.loads((root/(name+'-source.json')).read_text(encoding='utf-8-sig'))['sha']
    for p in sorted((root/'models').rglob('*')):
        if p.is_file():rows.append({'path':p.relative_to(root).as_posix(),'size':p.stat().st_size,'sha256':core.sha256_of(p)})
    for name in ('service.py','formula_service_v3.py'):
        p=root/name;rows.append({'path':name,'size':p.stat().st_size,'sha256':core.sha256_of(p)})
    lock={'model_revisions':pins,'files':rows,'python':core.PY,'scope':'local-only candidate inference',
          'runtime':'torch 2.4.1+cu124; transformers 4.45.2; ORT GPU/CPU 1.19.2',
          'licenses':{'qwen':'Apache-2.0','mfd':'MIT','mfr':'MIT'},
          'selected_ports':{'text_vision':18790,'formula':18794},'downloads_in_project':False}
    for name,text in [('start_local_ai.ps1',START),('stop_local_ai.ps1',STOP),('README.md',README)]:
        (root/name).write_text(text,encoding='utf-8')
    (root/'model_lock.json').write_text(json.dumps(lock,ensure_ascii=False,indent=2),encoding='utf-8')
    rd=audit.run_dir(local_closure.args());core.write_json(rd/'quality/local_model_runtime.json',
        {'external_directory':str(root),'model_revisions':pins,'model_lock_sha256':core.sha256_of(root/'model_lock.json'),
         'selected_ports':lock['selected_ports'],'model_files':len(rows),'private_credentials_in_report':False,
         'input_leaves_machine':False,'semantic_correctness_claimed':False})
    print({'external_operations_installed':True,'model_files':len(rows),'pins':pins})

if __name__=='__main__':install()
