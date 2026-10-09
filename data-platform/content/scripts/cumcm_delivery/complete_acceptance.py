"""Complete remaining acceptance after the real publication test restores full v2."""
import asyncio,subprocess,time,urllib.request
from . import core,qa_publish,qa_consumer,qa_readonly,qa_incremental,qa_rendering,final_report

def main():
    report=core.CONTENT_DIR/'reports/trae/cumcm_codex_publication.json'
    deadline=time.monotonic()+7200
    while not report.exists():
        if time.monotonic()>deadline:raise ValueError('publication test did not finish; do not claim acceptance')
        print('waiting for full-release rollback verification to finish',flush=True)
        time.sleep(30)
    if asyncio.run(qa_publish.pointer())!=qa_publish.FULL:raise ValueError('full release was not restored')
    qa_incremental.main()
    print('actual synthetic cross-version incremental checks completed',flush=True)
    command=['pwsh','-NoProfile','-File',str(core.CONTENT_DIR/'scripts/start_cumcm_api.ps1')]
    subprocess.run(command,cwd=str(core.CONTENT_DIR),check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18789/health',timeout=2) as response:
                if response.status==200:break
        except (OSError,TimeoutError):time.sleep(1)
    else:raise ValueError('local API did not become ready')
    qa_consumer.main()
    print('final consumer and active-release discovery checks completed',flush=True)
    qa_readonly.main()
    print('full dry-run and read-only before/after checks completed',flush=True)
    final_report.main()
    print('final acceptance artifacts ready',flush=True)

if __name__=='__main__':main()
