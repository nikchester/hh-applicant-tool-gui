"""UI-independent action mapping and nonblocking worker lifecycle."""

import queue
import subprocess
import threading
from uuid import uuid4

from gui_protocol import (ACTIONS, Cancelled, MAX_FRAME, ProtocolError, ReviewState,
                          decode, encode, frame, validate_done)
from gui_runtime import (GATEWAY_ACTIONS, Gateway, RunBusy, RunMutex, process_options,
                         provider_uses_gateway, worker_command)
from gui_owned_process import owned_job
from gui_recovery import MUTATING_ACTIONS, RecoveryMarker


def action_settings(action, *, resume="", search="", all_resumes=False,
                    limit=1, min_pause=60, max_pause=180, consent=False, no_letter=False):
    if action not in ACTIONS:
        raise ValueError("Unknown GUI action")
    if action not in {"preview", "apply", "auto"}:
        return {}
    settings = {"resume": resume, "search": search,
                "all": all_resumes if action != "preview" else False}
    if action in {"apply", "auto"}:
        settings["no_letter"] = bool(no_letter)
    if action == "auto":
        settings.update(limit=limit, min_pause=min_pause, max_pause=max_pause, consent=consent)
    return settings


class Session:
    def __init__(self, action, settings, *, popen=subprocess.Popen, mutex_factory=RunMutex,
                 gateway_factory=Gateway, job_factory=owned_job, marker_factory=RecoveryMarker,
                 recovery_acknowledgement=None):
        self.run = uuid4().hex
        self.action, self.settings = action, settings
        self.state = ReviewState(self.run)
        self.events, self.outgoing = queue.Queue(), queue.Queue()
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.process = None
        self.forced = False
        self.popen, self.mutex_factory, self.gateway_factory = popen, mutex_factory, gateway_factory
        self.job_factory, self.marker_factory = job_factory, marker_factory
        self.recovery_acknowledgement = recovery_acknowledgement

    def notify(self, name, data):
        self.events.put((name, data))

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def respond(self, action, text=""):
        self.outgoing.put(self.state.respond(action, text))

    def cancel(self):
        if not self.cancelled.is_set():
            self.cancelled.set()
            self.outgoing.put(self.state.cancel())

    def force(self):
        self.cancel()
        self.forced = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    def _write(self, process):
        try:
            while not self.finished.is_set():
                try:
                    data = self.outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                process.stdin.write(encode(data))
                process.stdin.flush()
        except (OSError, ValueError):
            self.cancelled.set()

    def _run(self):
        mutex = self.mutex_factory()
        gateway = self.gateway_factory(self.notify, self.cancelled)
        marker = self.marker_factory()
        success, done, busy = False, None, False
        try:
            mutex.acquire()  # Owned/released on this same thread, shared with CLI.
            if self.cancelled.is_set():
                raise Cancelled
            if self.action in MUTATING_ACTIONS:
                marker.begin(self.recovery_acknowledgement)
            job = self.job_factory()  # GUI membership precedes *any* owned child.
            # #9: no letter means no model, so ChatMock is not needed.
            if (self.action in GATEWAY_ACTIONS and not self.settings.get("no_letter")
                    and provider_uses_gateway()):
                gateway.start()
            if self.cancelled.is_set():
                raise Cancelled
            self.process = job.spawn(worker_command(action=self.action), popen=self.popen, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                bufsize=1, **process_options())
            process = self.process
            if self.cancelled.is_set():
                process.stdin.close()  # Worker sees EOF before receiving any action.
                raise Cancelled
            process.stdin.write(encode(frame(self.run, "start", {"action": self.action, "settings": self.settings})))
            process.stdin.flush()
            threading.Thread(target=self._write, args=(process,), daemon=True).start()
            while True:
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                try:
                    data = decode(line)
                    if data["run"] != self.run or done is not None:
                        raise ProtocolError("Wrong worker lifecycle")
                    if data["type"] == "done":
                        done = validate_done(data["payload"])
                    elif data["type"] in {"event", "review"}:
                        self.notify("frame", data)
                    else:
                        raise ProtocolError("Unexpected worker frame")
                except ProtocolError:
                    self.cancel()
                    self.notify("status", "Сбой связи с фоновым процессом. Запуск остановлен, неподтверждённые тексты не отправлены.")
            code = process.wait(timeout=5)
            success = (done is not None and done["success"] and not done["unknown"]
                       and code == 0 and not self.cancelled.is_set() and not self.forced)
        except Cancelled:
            pass
        except RunBusy:
            busy = True
            self.notify("status", "Уже идёт другой запуск HH: окно GUI или автоотклики в PowerShell. "
                                  "Дождись его завершения или останови его, затем повтори.")
        except (Exception, SystemExit):
            self.notify("status", "Запуск не удался. Повтор не выполняется сам.")
            self.cancel()
        finally:
            # Never release the mutex/start another action while a worker survives.
            process = self.process
            if process is not None:
                if process.poll() is None:
                    self.cancel()
                    self.notify("status", "Жду завершения текущего шага. Если ждать слишком долго, нажми «Прервать сразу…».")
                    process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass  # Broken worker pipes must not bypass gateway/lock cleanup.
            stopped = False
            try:
                stopped = gateway.close(success=success)
                if success and stopped and self.action in MUTATING_ACTIONS:
                    marker.complete()
            except OSError:
                self.notify("status", "Не удалось убрать временные файлы запуска. Перед новым запуском проверь отклики на hh.ru.")
            finally:
                mutex.release()
                self.finished.set()
                self.notify("finished", {"success": success and stopped, "done": done,
                                          "forced": self.forced, "cancelled": self.cancelled.is_set(),
                                          "busy": busy})
