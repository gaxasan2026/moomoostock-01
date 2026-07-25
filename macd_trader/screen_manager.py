"""
screen_manager.py
銘柄スクリーニング（screen.py）をバックグラウンドスレッドで実行し、
進捗をWeb GUIからポーリングできるようにする
"""
import threading
import uuid

from screen import run_screen


class ScreenJob:
    def __init__(self, job_id: str, symbol_id: str, start_date: str, end_date: str):
        self.job_id = job_id
        self.symbol_id = symbol_id
        self.start_date = start_date
        self.end_date = end_date
        self.status = "running"  # running / done / error
        self.progress = "開始しています..."
        self.error = None
        self.report_filename = None


class ScreenManager:
    def __init__(self):
        self._jobs: dict = {}
        self._lock = threading.Lock()

    def start(self, symbol_id: str, start_date: str, end_date: str,
              market: str = "US", timeframe: str = "K_1M",
              target_position_value: float = 800.0) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = ScreenJob(job_id, symbol_id, start_date, end_date)
        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job, market, timeframe, target_position_value),
            daemon=True, name=f"screen-{job_id}",
        )
        thread.start()
        return job_id

    def _run(self, job: ScreenJob, market: str, timeframe: str, target_position_value: float):
        def on_progress(msg):
            job.progress = msg

        try:
            out_path = run_screen(
                job.symbol_id, job.start_date, job.end_date,
                market=market, timeframe=timeframe,
                target_position_value=target_position_value,
                on_progress=on_progress,
            )
            job.report_filename = out_path.name
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)

    def get(self, job_id: str):
        return self._jobs.get(job_id)
