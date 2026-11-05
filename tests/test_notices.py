"""A Sellerboard announcement floating over the export page must not block the export clicks."""
from __future__ import annotations

import pytest

from app import sellerboard

PAGE = """<html><body>
<a id="report" href="#" onclick="document.title='clicked'" style="display:block;width:300px">Dashboard by product</a>
<div id="notice" style="position:fixed;inset:0;background:#fff;z-index:10">
  <div><b>sellerboard MCP update</b><p>Now you can let your connected AI assistant read ...</p>{close}</div>
</div>
</body></html>"""


@pytest.mark.parametrize("close", ['<button class="btn-close" onclick="this.closest(\'#notice\').remove()">×</button>', ""],
                         ids=["with-close-button", "without-close-button"])
def test_notice_over_the_page_is_closed_before_clicking(close):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(PAGE.replace("{close}", close))
            with pytest.raises(Exception):   # without the handler the notice swallows the click
                page.locator('text="Dashboard by product"').first.click(timeout=1_000)
            sellerboard._close_notices(page, {"notices": ["sellerboard MCP update"]})
            sellerboard._run_step(page, {"action": "click", "selector": 'text="Dashboard by product"'}, {}, 1)
            assert page.title() == "clicked"
        finally:
            browser.close()
