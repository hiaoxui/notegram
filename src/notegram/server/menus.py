"""Private, user-bound inline menus for managing chat/logger links."""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict

PAGE_SIZE = 6
MENU_TTL = 15 * 60
MAX_BUTTONS = 2000
THRESHOLDS = [("DEBUG", 10), ("INFO", 20), ("WARNING", 30), ("ERROR", 40), ("CRITICAL", 50)]


def label(text, length=48):
    # Keep user-supplied logger/chat names readable in buttons and plain-text panels.
    text = "".join(c if c.isprintable() else " " for c in str(text))
    return text if len(text) <= length else text[: length - 1] + "…"


class LinkMenus:
    def __init__(self, db, api, token):
        self.db, self.api = db, api
        self._fingerprint = hashlib.sha256(token.encode()).hexdigest()
        self._token = token
        self._buttons = OrderedDict()

    def authorized(self, user_id):
        return self.db.get_state(f"telegram_admin:{user_id}") == self._fingerprint

    def command(self, message, command, args):
        """Handle management commands; all other commands retain their old behavior."""
        if command not in {"auth", "logout", "links", "chats"}:
            return False
        chat, user = message["chat"], message.get("from", {})
        cid, uid = chat["id"], user.get("id")
        if chat.get("type") != "private" or uid != cid:
            self.api.send(
                cid, "Open a private conversation with me and use /auth TOKEN, then /links."
            )
            return True
        if command == "auth":
            if len(args) != 1:
                self.api.send(cid, "Usage: /auth TOKEN. Use your Notegram shared token.")
                return True
            # Do not leave the credential in private chat history when deletion is available.
            if "message_id" in message:
                self.api.delete(cid, message["message_id"])
            if not hmac.compare_digest(args[0].encode(), self._token.encode()):
                self.api.send(cid, "Authentication failed.")
                return True
            self.db.set_state(f"telegram_admin:{uid}", self._fingerprint)
        elif command == "logout":
            self.db.set_state(f"telegram_admin:{uid}", None)
            self.api.send(cid, "Management access revoked. Existing logger links are unchanged.")
            return True
        if not self.authorized(uid):
            self.api.send(cid, "Authenticate with /auth TOKEN to manage chat/logger links.")
            return True
        if args and command != "auth":
            self.api.send(cid, "Use /links without arguments to open the management panel.")
            return True
        self._render(uid, cid, None, {"action": "chats", "page": 0})
        return True

    def _button(self, title, uid, origin, **state):
        now = time.monotonic()
        while self._buttons and (
            next(iter(self._buttons.values()))["expires"] <= now
            or len(self._buttons) >= MAX_BUTTONS
        ):
            self._buttons.popitem(last=False)
        key = "ng:" + secrets.token_urlsafe(12)
        self._buttons[key] = dict(user=uid, origin=origin, expires=now + MENU_TTL, state=state)
        return {"text": label(title), "callback_data": key}

    def callback(self, query):
        qid = query.get("id")
        message = query.get("message", {})
        uid = query.get("from", {}).get("id")
        cid = message.get("chat", {}).get("id")
        data = query.get("data", "")
        button = self._buttons.get(data) if isinstance(data, str) else None
        if not button or button["expires"] <= time.monotonic():
            self.api.answer(qid, "This menu expired. Open /links again.", alert=True)
            return
        if (
            uid != button["user"]
            or cid != button["origin"]
            or cid != uid
            or message.get("chat", {}).get("type") != "private"
            or not self.authorized(uid)
        ):
            self.api.answer(
                qid, "This menu belongs to an authenticated user. Use /auth in private.", alert=True
            )
            return
        # Acknowledge promptly, before paced edits or storage work.
        self.api.answer(qid)
        state = button["state"]
        action = state["action"]
        target, domain = state.get("target"), state.get("domain")
        if action in {"set", "create", "remove"}:
            # Consume mutation buttons: replaying a confirmation cannot remove a later re-link.
            self._buttons.pop(data, None)
            if action == "set":
                self.db.set_level(domain, target, state["level"])
                state = dict(action="detail", target=target, domain=domain)
            elif action == "create":
                # Do not overwrite a threshold changed since the add-logger page was opened.
                if self.db.link_status(domain, target) is None:
                    self.db.link_chat(domain, target)
                state = dict(action="detail", target=target, domain=domain)
            else:
                self.db.unlink_chat(domain, target)
                state = dict(action="links", target=target)
        self._render(uid, cid, message["message_id"], state)

    def _render(self, uid, origin, message_id, state):
        action = state["action"]
        target, domain = state.get("target"), state.get("domain")
        page = max(0, state.get("page", 0))
        rows = []

        def button(title, action, **kwargs):
            return self._button(title, uid, origin, action=action, **kwargs)

        def paginate(items, make_row, action, **kwargs):
            nonlocal page
            page = min(page, max(0, (len(items) - 1) // PAGE_SIZE))
            for item in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                rows.append(make_row(item))
            nav = []
            if page:
                nav.append(button("‹ Previous", action, page=page - 1, **kwargs))
            if (page + 1) * PAGE_SIZE < len(items):
                nav.append(button("Next ›", action, page=page + 1, **kwargs))
            if nav:
                rows.append(nav)
            return f"Page {page + 1} of {max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)}"

        if action == "chats":
            chats = self.db.chats()
            heading = paginate(
                chats,
                lambda c: [
                    button(f"{c['title']} · {c['links']} loggers", "links", target=c["chat_id"])
                ],
                "chats",
            )
            text = (
                "Chat / logger links\n\nChoose a chat to manage its loggers.\n"
                f"{heading}\n\nTo register a group, add me and send /links there."
            )
            rows.append([button("Refresh", "chats", page=page)])
        elif action in {"links", "add"}:
            chats = {c["chat_id"]: c["title"] for c in self.db.chats()}
            title = label(chats.get(target, target), 100)
            links = self.db.subscriptions(target)
            if action == "links":
                heading = paginate(
                    links,
                    lambda s: [
                        button(
                            f"{s['domain']} · {self._level(s['level'])}",
                            "detail",
                            target=target,
                            domain=s["domain"],
                        )
                    ],
                    "links",
                    target=target,
                )
                text = f"{title} ({target})\n\nLinked loggers: {len(links)}\n{heading}"
                if not links:
                    text += "\nNo linked loggers. Choose Add logger to start."
                rows.append([button("＋ Add logger", "add", target=target)])
                rows.append(
                    [
                        button("‹ All chats", "chats"),
                        button("Refresh", "links", target=target, page=page),
                    ]
                )
            else:
                linked = {s["domain"] for s in links}
                choices = [d for d in self.db.loggers() if d not in linked]
                heading = paginate(
                    choices,
                    lambda d: [button(d, "new", target=target, domain=d)],
                    "add",
                    target=target,
                )
                text = f"Add a logger to {title} ({target})\n\n{heading}"
                if not choices:
                    text += (
                        "\nNo other known loggers. Send a log event first, or use "
                        "/link LOGGER TOKEN to create a link before its first event."
                    )
                rows.append([button("‹ Linked loggers", "links", target=target)])
        elif action in {"detail", "confirm", "new"}:
            link = self.db.link_status(domain, target)
            text = f"Logger: {label(domain, 200)}\nChat: {target}\n\n"
            if action == "new" and link is None:
                text += "Link this logger at WARNING and above?"
                rows.append([button("Link at WARNING", "create", target=target, domain=domain)])
            elif link is None:
                text += "This logger is no longer linked."
            elif action == "confirm":
                text += (
                    "Unlink this logger? Pending and failed deliveries for this link "
                    "will be removed. Stored log history is kept."
                )
                rows.append(
                    [
                        button("Confirm unlink", "remove", target=target, domain=domain),
                        button("Cancel", "detail", target=target, domain=domain),
                    ]
                )
            else:
                text += (
                    f"Threshold: {self._level(link['level'])} and above\n"
                    f"Pending deliveries: {link['pending']}\nFailed deliveries: {link['failed']}\n\n"
                    "Choose a threshold for future events:"
                )
                levels = [
                    button(
                        ("✓ " if value == link["level"] else "") + name,
                        "set",
                        target=target,
                        domain=domain,
                        level=value,
                    )
                    for name, value in THRESHOLDS
                ]
                rows.extend([levels[:3], levels[3:]])
                rows.append([button("Unlink…", "confirm", target=target, domain=domain)])
            rows.append([button("‹ Linked loggers", "links", target=target)])
        else:
            return
        markup = {"inline_keyboard": rows}
        if message_id is None:
            self.api.send(origin, text, reply_markup=markup)
        else:
            self.api.edit(origin, message_id, text, reply_markup=markup)

    @staticmethod
    def _level(level):
        return next((name for name, value in THRESHOLDS if value == level), str(level))
