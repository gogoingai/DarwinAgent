"""Recorded web fixtures and bounded fake HTTP transport; no test scenarios."""

HTML = "<html><title>Python at DuckDuckGo</title><input name='q' value='Python'><a class='result-link' href='/l/?uddg=https%3A%2F%2Fwww.python.org%2F'>Official Python</a><td class='result-snippet'>Python &amp; docs</td></html>"


def page(body=HTML, content_type="text/html"):
    return {
        "url": "https://lite.duckduckgo.com/lite/?q=Python",
        "body": body,
        "content_type": content_type,
    }


def dns(ip="93.184.216.34"):
    return [(2, 1, 6, "", (ip, 443))]


class Response:
    def __init__(self, status=200, body=b"hello", headers=None):
        from email.message import Message

        self.status, self.body = status, body
        self.headers = Message()
        for key, value in (headers or {"Content-Type": "text/plain"}).items():
            self.headers[key] = value

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read(self, size):
        return self.read1(size)

    def read1(self, size):
        result, self.body = self.body[:size], self.body[size:]
        return result


class Connection:
    responses = []
    calls = []

    def __init__(self, *args):
        self.calls.append(args)
        self.sock = self

    def settimeout(self, timeout):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass
