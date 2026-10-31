"""Отправка писем через SMTP вашей почты: по одному, с паузами и дневным лимитом."""
import random
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


class AuthProblem(Exception):
    pass


def build_message(cfg, to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.smtp_user))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = cfg.smtp_user
    msg["Date"] = formatdate(localtime=True)
    domain = cfg.smtp_user.split("@")[-1] if "@" in cfg.smtp_user else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["List-Unsubscribe"] = f"<mailto:{cfg.smtp_user}?subject=unsubscribe>"
    msg.set_content(body)
    return msg


def smtp_send(cfg, msg: EmailMessage, smtp_factory=None):
    ctx = ssl.create_default_context()
    if smtp_factory:
        server = smtp_factory(cfg)
    elif cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        server.starttls(context=ctx)
    try:
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise AuthProblem(str(exc)) from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


def check_login(cfg, smtp_factory=None) -> str:
    """Проверяет вход в почту. Возвращает пустую строку или текст ошибки."""
    try:
        ctx = ssl.create_default_context()
        if smtp_factory:
            server = smtp_factory(cfg)
        elif cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
            server.starttls(context=ctx)
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.quit()
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


class BatchSender:
    """Фоновая отправка одобренных писем."""

    def __init__(self, cfg, store, drafter, notify, smtp_factory=None, sleep=time.sleep):
        self.cfg = cfg
        self.store = store
        self.drafter = drafter
        self.notify = notify
        self.smtp_factory = smtp_factory
        self.sleep = sleep
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> str:
        if self.running:
            return "Рассылка уже идёт."
        queue = [r for r in self.store.by_status("approved") if r["email"]]
        left = self.cfg.daily_email_limit - self.store.sent_today()
        if not queue:
            return "В очереди писем пусто: одобрите организации с почтой во вкладке «Новые»."
        if left <= 0:
            return f"Дневной лимит ({self.cfg.daily_email_limit} писем) уже исчерпан. Продолжим завтра."
        count = min(len(queue), left)
        avg = (self.cfg.email_delay_min + self.cfg.email_delay_max) / 2
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, args=(queue[:count],), daemon=True)
        self.thread.start()
        return (f"Начинаю рассылку: {count} писем, примерно {max(1, round(count * avg / 60))} мин. "
                f"Не закрывайте окно бота.")

    def stop(self):
        self.stop_event.set()

    def run(self, queue):
        sent = failed = 0
        for i, lead in enumerate(queue):
            if self.stop_event.is_set():
                break
            # свежая проверка: вдруг лида уже убрали или внесли в чёрный список
            fresh = self.store.get(lead["id"])
            if not fresh or fresh["status"] != "approved" or self.store.is_blacklisted(fresh["email"]):
                continue
            subject, body = self.drafter.email(fresh)
            try:
                smtp_send(self.cfg, build_message(self.cfg, fresh["email"], subject, body),
                          self.smtp_factory)
            except AuthProblem as exc:
                self.store.log_send(fresh["id"], fresh["email"], False, str(exc))
                self.notify("⛔ Почта не пускает: неверный логин или пароль приложения. "
                            "Рассылка остановлена, проверьте SMTP_USER и SMTP_PASSWORD в .env.")
                return
            except smtplib.SMTPRecipientsRefused as exc:
                self.store.set_status(fresh["id"], "failed", "адрес отклонён почтовым сервером")
                self.store.log_send(fresh["id"], fresh["email"], False, str(exc))
                failed += 1
            except Exception as exc:  # сеть, лимиты почтовика и т.д.
                self.store.log_send(fresh["id"], fresh["email"], False, str(exc))
                failed += 1
                if failed >= 3 and sent == 0:
                    self.notify(f"⛔ Три ошибки подряд, останавливаюсь. Последняя: {exc}")
                    return
            else:
                self.store.set_status(fresh["id"], "sent")
                self.store.log_send(fresh["id"], fresh["email"], True)
                sent += 1
                if sent % 5 == 0:
                    self.notify(f"📨 Отправлено {sent} из {len(queue)}")
            if i < len(queue) - 1 and not self.stop_event.is_set():
                self._pause(random.uniform(self.cfg.email_delay_min, self.cfg.email_delay_max))

        stopped = " (остановлено вами)" if self.stop_event.is_set() else ""
        self.notify(f"✅ Рассылка завершена{stopped}: отправлено {sent}, ошибок {failed}. "
                    f"Ответы придут на {self.cfg.smtp_user}.")

    def _pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop_event.is_set():
            self.sleep(min(1.0, max(0.0, end - time.monotonic())))
