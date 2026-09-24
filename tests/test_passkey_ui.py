from __future__ import annotations

from kahin.passkey_ui import configure_unlocked_vault, prepare_login_ui


class FakeElement:
    def __init__(self, client, *, selector="", value=None, text="", click=None, children=None):
        self.client = client
        self.selector = selector
        self.value = value
        self.text = text
        self._click = click
        self.children = children or []

    def click(self):
        if self._click:
            self._click()
        elif self.selector.startswith("bit-session-timeout-input bit-select"):
            self.client.dropdown_open = True
        elif self.selector == "#use-passkeys":
            self.client.passkeys = not self.client.passkeys
            self.client.passkey_clicks += 1

    def get_attribute(self, name):
        if name in {"value", "data-testid", "class", "aria-label", "type"}:
            return self.value if name == "value" else None
        return None

    def find_elements(self, by, selector):
        assert by == "css selector"
        if selector == "button":
            return self.children
        return []


class FakeMarionette:
    def __init__(self, *, logged_in=True, locked=False, unlocked_nudge=False, passkeys=False, timeout="fiveMinutes"):
        self.logged_in = logged_in
        self.login_visible = not logged_in
        self.locked = locked
        self.unlocked_nudge = unlocked_nudge
        self.passkeys = passkeys
        self.passkey_clicks = 0
        self.timeout = timeout
        self.pending_timeout = None
        self.dropdown_open = False
        self.url = ""
        self.visited = []
        self.actions = []
        self.login_continue_clicks = 0
        self.login_submit_clicks = 0

    def navigate(self, url):
        self.visited.append(url)
        base, _fragment = url.split("#", 1) if "#" in url else (url, "")
        route = url.split("#", 1)[1].strip("/") if "#" in url else ""
        if route == "" and not self.logged_in:
            self.url = f"{base}#/intro-carousel"
        elif route == "account-security" and (not self.logged_in or self.locked):
            self.url = f"{base}#/login"
            self.login_visible = True
        else:
            self.url = url

    @property
    def current_url(self):
        return self.url

    def find_element(self, by, selector):
        if by == "xpath":
            if "'skip'" in selector.lower() and not self.logged_in and "intro-carousel" in self.url:
                return FakeElement(self, text="Skip", click=lambda: self.actions.append("skip"))
            if "'log in'" in selector.lower() and not self.logged_in and "intro-carousel" in self.url:
                return FakeElement(
                    self,
                    text="Log in",
                    click=lambda: self._open_login(),
                )
            raise LookupError(selector)

        if selector in {
            "input[type='email'], input[autocomplete='username'], input[name='email'], #email",
            "input[type='email']",
            "input[autocomplete='username']",
            "input[name='email']",
            "#email",
        } and self.login_visible:
            return FakeElement(self, selector=selector)
        if selector == "button[data-testid='login-continue-button']" and self.login_visible:
            return FakeElement(self, selector=selector, click=self._login_continue)
        if selector == "button[data-testid='login-submit-button']" and self.login_visible:
            return FakeElement(self, selector=selector, click=self._login_submit)
        if selector == "input[type='password']" and self.login_visible:
            return FakeElement(self, selector=selector)
        if selector == "#masterPassword, input[name='masterPassword']" and self.login_visible:
            return FakeElement(self, selector=selector)
        if selector in {
            "bit-session-timeout-input bit-select",
            "bit-session-timeout-input bit-select ng-select",
        } and self.logged_in and "account-security" in self.url:
            return FakeElement(self, selector=selector)
        if selector == "bit-callout" and self.locked and "account-security" in self.url:
            return FakeElement(self, selector=selector)
        if selector == "bit-callout" and self.unlocked_nudge and "account-security" in self.url:
            return FakeElement(self, selector=selector)
        if selector == "#use-passkeys" and self.logged_in and "notifications" in self.url:
            return FakeElement(self, selector=selector)
        if selector in {"form[bit-simple-dialog]", "form[bit-simple-dialog] button[type='submit']"} and self.pending_timeout:
            confirm = FakeElement(
                self,
                selector="confirm-submit",
                click=self._confirm_timeout,
            )
            return confirm if "button" in selector else FakeElement(self, selector=selector, children=[confirm])
        raise LookupError(selector)

    def find_elements(self, by, selector):
        assert by == "css selector"
        if selector == ".ng-dropdown-panel .ng-option" and self.dropdown_open:
            return [
                FakeElement(self, selector="ng-option", text="Five minutes"),
                FakeElement(self, selector="ng-option", text="Never", click=self._choose_never),
            ]
        return []

    def execute_script(self, script):
        if "bit-session-timeout-input bit-select" in script:
            return {"never": "Never", "asla": "Asla"}.get(self.timeout.lower(), self.timeout)
        if "#use-passkeys" in script:
            return self.passkeys
        raise AssertionError(f"unexpected script: {script}")

    def _choose_never(self):
        self.dropdown_open = False
        self.pending_timeout = "never"

    def _confirm_timeout(self):
        self.timeout = self.pending_timeout
        self.pending_timeout = None

    def _open_login(self):
        self.actions.append("intro log in")
        self.url = self.url.split("#", 1)[0] + "#/login"

    def _login_continue(self):
        self.login_continue_clicks += 1

    def _login_submit(self):
        self.login_submit_clicks += 1


def test_prepare_login_ui_skips_prompt_and_opens_login_without_credentials():
    m = FakeMarionette(logged_in=False)

    result = prepare_login_ui(m, "moz-extension://bitwarden/popup.html")

    assert result == {"status": "ready", "route": "login", "state": "login"}
    assert m.actions == ["skip", "intro log in"]
    assert m.visited == ["moz-extension://bitwarden/popup.html"]
    assert all("password" not in key.lower() for key in result)


def test_configure_unlocked_vault_sets_and_verifies_both_options():
    m = FakeMarionette()

    result = configure_unlocked_vault(m, "moz-extension://bitwarden/popup.html")

    assert result == {
        "status": "configured",
        "route": "notifications",
        "state": "verified",
        "settings": {"vault_timeout": "never", "ask_to_save_and_use_passkeys": True},
    }
    assert m.timeout == "never"
    assert m.passkeys is True
    assert m.visited == [
        "moz-extension://bitwarden/popup.html#/account-security",
        "moz-extension://bitwarden/popup.html#/notifications",
        "moz-extension://bitwarden/popup.html#/account-security",
        "moz-extension://bitwarden/popup.html#/notifications",
    ]


def test_configure_unlocked_vault_keeps_default_enabled_passkeys_checked():
    m = FakeMarionette(passkeys=True)

    result = configure_unlocked_vault(m, "moz-extension://bitwarden/popup.html")

    assert result["status"] == "configured"
    assert m.passkey_clicks == 0


def test_prepare_login_ui_recognizes_email_first_login_without_clicking_login_submit():
    m = FakeMarionette(logged_in=False)

    result = prepare_login_ui(m, "moz-extension://bitwarden/popup.html")

    assert result == {"status": "ready", "route": "login", "state": "login"}
    assert m.current_url.endswith("#/login")
    assert m.login_continue_clicks == 0
    assert m.login_submit_clicks == 0
    assert m.actions == ["skip", "intro log in"]


def test_configure_unlocked_vault_accepts_unlocked_account_security_nudge():
    m = FakeMarionette(unlocked_nudge=True)

    result = configure_unlocked_vault(m, "moz-extension://bitwarden/popup.html")

    assert result["status"] == "configured"
    assert m.timeout == "never"
    assert m.passkeys is True


def test_configure_unlocked_vault_does_not_change_redirected_or_unauthenticated_state():
    locked = FakeMarionette(locked=True)
    locked_result = configure_unlocked_vault(locked, "moz-extension://bitwarden/popup.html")
    assert locked_result == {
        "status": "login_required",
        "route": "account-security",
        "state": "login",
    }
    assert locked.timeout == "fiveMinutes"

    signed_out = FakeMarionette(logged_in=False)
    signed_out_result = configure_unlocked_vault(signed_out, "moz-extension://bitwarden/popup.html")
    assert signed_out_result == {
        "status": "login_required",
        "route": "account-security",
        "state": "login",
    }
    assert signed_out.timeout == "fiveMinutes"
    assert signed_out.passkeys is False
