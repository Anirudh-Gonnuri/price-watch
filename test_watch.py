"""Offline tests for watch.py (parsing and alerting). Run: python3 -m unittest -v"""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch

URL = "https://www.amazon.in/dp/B0DSKL9MQ8"
MARKER = watch.AMAZON_MARKER


def respond(body: str):
    """Patch urlopen so fetch_once receives `body` without touching the network."""
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = body.encode()
    return mock.patch("urllib.request.urlopen", return_value=resp)


class FetchClassification(unittest.TestCase):
    def test_normal_page_is_returned(self):
        with respond('<span id="productTitle">Galaxy</span>'):
            self.assertIn("Galaxy", watch.fetch_once(URL, MARKER))

    def test_captcha_is_blocked_not_layout_change(self):
        with respond("<form>Type the characters you see: captcha</form>"):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL, MARKER)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_bot_check_challenge_is_blocked_not_layout_change(self):
        body = "<meta http-equiv=\"refresh\" content=\"5; URL='/dp/x?bm-verify=AAQ'\">"
        with respond(body):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL, MARKER)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_unrecognised_page_is_layout_change(self):
        with respond("<html><h1 class='new-title'>Galaxy</h1></html>"):
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch_once(URL, MARKER)

    def test_layout_change_is_not_retried(self):
        with mock.patch.object(watch, "fetch_once",
                               side_effect=watch.LayoutChanged("x")) as f:
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch(URL, MARKER)
        self.assertEqual(f.call_count, 1)


class AmazonParsing(unittest.TestCase):
    VARIANTS = {
        "B0DSKL9MQ8": ["12GB + 256GB", "No cost EMI", "Titanium Whitesilver"],
        "B0DSBTKP5Q": ["12GB + 512GB", "No cost EMI", "Titanium Silverblue"],
    }

    def test_no_match_when_colour_only_exists_in_other_size(self):
        self.assertIsNone(watch.find_variant(self.VARIANTS, ["512GB", "Whitesilver"]))

    def test_match_ignores_case_and_spacing(self):
        self.assertEqual(watch.find_variant(self.VARIANTS, ["512 gb", "silver blue"]),
                         "B0DSBTKP5Q")

    def test_missing_variant_table_is_layout_change(self):
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_variants('<span id="productTitle">x</span>')

    def test_price_parsed_as_whole_rupees(self):
        page = ('<span id="productTitle"> S25 </span><span class="a-price-whole">1,09,999'
                '</span><div id="add-to-cart-button"></div>')
        self.assertEqual(watch.parse_amazon_product(page)[1], 109999)


def flipkart_page(price, availability="InStock", name="Galaxy S25 Ultra (Whitesilver, 512 GB)"):
    product = {"@context": "https://schema.org", "@type": "Product", "name": name,
               "offers": {"price": price, "priceCurrency": "INR",
                          "availability": f"https://schema.org/{availability}"}}
    return ('<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>'
            f'<script type="application/ld+json">{json.dumps(product)}</script>')


class FlipkartParsing(unittest.TestCase):
    URL = "https://www.flipkart.com/x/p/itm1"

    def check(self, page, max_price=109999):
        with mock.patch.object(watch, "fetch", return_value=page):
            return watch.check_flipkart(self.URL, max_price)

    def test_reads_product_block_among_other_json_ld(self):
        name, price, in_stock = watch.parse_flipkart_product(flipkart_page(139999))
        self.assertEqual((price, in_stock), (139999, True))
        self.assertIn("Whitesilver", name)

    def test_above_target_is_not_good(self):
        r = self.check(flipkart_page(139999))
        self.assertEqual((r.status, r.good), ("above_target", False))

    def test_exactly_at_target_is_good(self):
        r = self.check(flipkart_page(109999))
        self.assertEqual((r.status, r.good), ("at_target", True))
        self.assertEqual(r.status_text, "at ₹1,09,999 (target ₹1,09,999)")

    def test_cheap_but_sold_out_is_not_good(self):
        r = self.check(flipkart_page(99999, availability="OutOfStock"))
        self.assertEqual((r.status, r.good), ("out_of_stock", False))

    def test_decimal_and_string_prices(self):
        self.assertEqual(watch.parse_flipkart_product(flipkart_page("109999.00"))[1], 109999)

    def test_missing_product_block_is_layout_change(self):
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_flipkart_product('<script type="application/ld+json">{}</script>')

    def test_missing_price_is_layout_change(self):
        page = flipkart_page(1).replace('"price": 1, ', "")
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_flipkart_product(page)


class StockAndPriceRule(unittest.TestCase):
    rule = staticmethod(watch.stock_and_price_result)

    def test_no_max_price_means_alert_when_in_stock(self):
        r = self.rule("x", 50000, True, None, URL)
        self.assertEqual((r.status, r.good, r.status_text), ("in_stock", True, "in stock"))

    def test_sold_out_is_never_good(self):
        self.assertFalse(self.rule("x", 1, False, 99999, URL).good)
        self.assertFalse(self.rule("x", 1, False, None, URL).good)

    def test_missing_price_never_counts_as_at_target(self):
        r = self.rule("x", None, True, 99999, URL)
        self.assertEqual((r.status, r.good), ("above_target", False))

    def test_amazon_product_mode_uses_the_rule(self):
        page = ('<span id="productTitle"> Phone </span><span class="a-price-whole">95,000'
                '</span><div id="availability"> In stock </div><div id="add-to-cart-button">')
        with mock.patch.object(watch, "fetch", return_value=page):
            r = watch.check_amazon_product("www.amazon.in", "B0X", 99999)
        self.assertEqual((r.status, r.price, r.url),
                         ("at_target", 95000, "https://www.amazon.in/dp/B0X?th=1&psc=1"))

    def test_build_target_picks_amazon_mode_by_keys(self):
        entry = {"key": "a", "site": "amazon", "label": "x", "host": "www.amazon.in"}
        with mock.patch.object(watch, "check_amazon_product") as product, \
             mock.patch.object(watch, "check_amazon") as variant:
            watch.build_target({**entry, "asin": "B0X"}).check()
            watch.build_target({**entry, "listing": "B0Y", "match": ["z"]}).check()
        product.assert_called_once_with("www.amazon.in", "B0X", None)
        variant.assert_called_once_with("www.amazon.in", "B0Y", ["z"])


class LinkParsing(unittest.TestCase):
    def test_amazon_long_link_with_tracking(self):
        e = watch.parse_link("https://www.amazon.in/Samsung-Smartphone-Whitesilver/dp/B0DSKL9MQ8"
                             "/ref=pd_ci_mcx?pd_rd_w=naHTu&th=1")
        self.assertEqual(e, {"key": "amazon-b0dskl9mq8", "site": "amazon",
                             "host": "www.amazon.in", "asin": "B0DSKL9MQ8"})

    def test_amazon_gp_product_and_no_scheme(self):
        self.assertEqual(watch.parse_link("amazon.in/gp/product/B0DSBTKP5Q")["asin"],
                         "B0DSBTKP5Q")

    def test_flipkart_keeps_only_pid(self):
        e = watch.parse_link("https://www.flipkart.com/samsung-s25/p/itm2ce28bbf75993"
                             "?pid=MOBH8K8UVQCSQE3Z&lid=LST123&marketplace=FLIPKART&q=s25")
        self.assertEqual(e["url"],
                         "https://www.flipkart.com/samsung-s25/p/itm2ce28bbf75993"
                         "?pid=MOBH8K8UVQCSQE3Z")
        self.assertEqual(e["key"], "flipkart-mobh8k8uvqcsqe3z")

    def test_short_links_are_resolved_first(self):
        with mock.patch.object(watch, "resolve_short_link",
                               return_value="https://www.amazon.in/dp/B0DSKL9MQ8") as r:
            self.assertEqual(watch.parse_link("https://amzn.in/d/abc123")["asin"],
                             "B0DSKL9MQ8")
        r.assert_called_once()

    def test_rejects_other_sites_and_non_product_pages(self):
        for bad in ("https://www.amazon.com/dp/B0DSKL9MQ8", "https://www.myntra.com/x",
                    "https://www.amazon.in/s?k=phone", "https://www.flipkart.com/search?q=x"):
            with self.subTest(bad), self.assertRaises(ValueError):
                watch.parse_link(bad)

    def test_prices_in_any_common_format(self):
        for text in ("109999", "1,09,999", "₹1,09,999", "109999.00"):
            self.assertEqual(watch.parse_price(text), 109999)
        with self.assertRaises(ValueError):
            watch.parse_price("cheap")

    def test_short_label_drops_marketing_tail(self):
        self.assertEqual(
            watch.short_label("Samsung Galaxy S25 Ultra 5G (Titanium Whitesilver, 12GB RAM, "
                              "512GB Storage), 200MP Camera, 5000mAh Battery"),
            "Samsung Galaxy S25 Ultra 5G (Titanium Whitesilver, 12GB RAM, 512GB Storage)")
        self.assertEqual(watch.short_label("Noise Buds X, ANC, 50h playtime"), "Noise Buds X")


class ManageCommands(unittest.TestCase):
    """add/remove edit targets.json and sync GitHub, without touching the network."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        mock.patch.object(watch, "CONFIG_FILE", d / "targets.json").start()
        mock.patch.object(watch, "STATE_FILE", d / "state.json").start()
        self.sync = mock.patch.object(watch, "sync_github").start()
        mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)

    def add(self, url, max_price=None):
        result = watch.Result("in_stock", good=True, status_text="in stock",
                              title="S25", price=109999, url=url)
        with mock.patch.object(watch, "build_target") as build:
            build.return_value.check.return_value = result
            return watch.cmd_add(argparse.Namespace(url=url, max_price=max_price, label=None))

    def test_add_saves_entry_with_label_and_price_then_syncs(self):
        self.assertEqual(self.add("https://www.amazon.in/dp/B0DSKL9MQ8", "1,05,000"), 0)
        [entry] = watch.load_entries()
        self.assertEqual((entry["asin"], entry["max_price"], entry["label"]),
                         ("B0DSKL9MQ8", 105000, "S25"))
        self.sync.assert_called_once()

    def test_adding_same_link_updates_instead_of_duplicating(self):
        self.add("https://www.amazon.in/dp/B0DSKL9MQ8", "105000")
        self.add("https://www.amazon.in/dp/B0DSKL9MQ8", "99000")
        self.assertEqual([e["max_price"] for e in watch.load_entries()], [99000])

    def test_blocked_page_adds_nothing(self):
        with mock.patch.object(watch, "build_target") as build:
            build.return_value.check.side_effect = watch.Blocked("CAPTCHA")
            code = watch.cmd_add(argparse.Namespace(
                url="https://www.amazon.in/dp/B0DSKL9MQ8", max_price=None, label=None))
        self.assertEqual((code, watch.load_entries()), (2, []))
        self.sync.assert_not_called()

    def test_remove_by_number_or_key_prefix_and_drops_state(self):
        self.add("https://www.amazon.in/dp/B0DSKL9MQ8")
        self.add("https://www.amazon.in/dp/B0DSBTKP5Q")
        watch.save_state({"targets": {"amazon-b0dskl9mq8": {"status": "in_stock"}}})
        self.assertEqual(watch.cmd_remove(argparse.Namespace(target="1")), 0)
        self.assertEqual(watch.cmd_remove(argparse.Namespace(target="amazon-b0dsb")), 0)
        self.assertEqual(watch.load_entries(), [])
        self.assertEqual(watch.load_state()["targets"], {})

    def test_ambiguous_remove_changes_nothing(self):
        self.add("https://www.amazon.in/dp/B0DSKL9MQ8")
        self.add("https://www.amazon.in/dp/B0DSBTKP5Q")
        self.assertEqual(watch.cmd_remove(argparse.Namespace(target="amazon-")), 1)
        self.assertEqual(len(watch.load_entries()), 2)


class Formatting(unittest.TestCase):
    def test_indian_digit_grouping(self):
        cases = {999: "₹999", 1000: "₹1,000", 109999: "₹1,09,999", 12345678: "₹1,23,45,678"}
        for rupees, text in cases.items():
            self.assertEqual(watch.format_inr(rupees), text)


class Config(unittest.TestCase):
    def test_example_config_loads_every_kind_of_target(self):
        targets = watch.load_targets(watch.HERE / "targets.example.json")
        self.assertEqual([t.site for t in targets], ["Flipkart", "Amazon", "Amazon"])

    def test_missing_config_means_nothing_to_watch(self):
        self.assertEqual(watch.load_entries(watch.HERE / "no-such-file.json"), [])

    def test_unknown_site_rejected(self):
        with self.assertRaises(ValueError):
            watch.build_target({"key": "a", "site": "ebay", "label": "x"})

    def test_duplicate_keys_rejected(self):
        entry = {"key": "a", "site": "amazon", "label": "x", "host": "h",
                 "listing": "B0", "match": ["y"]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "targets.json"
            path.write_text(json.dumps({"targets": [entry, entry]}))
            with self.assertRaises(ValueError):
                watch.load_targets(path)


class StateMigration(unittest.TestCase):
    def test_single_product_state_moves_under_its_key(self):
        old = {"status": "in_stock", "asin": "B0X", "price": "₹1,09,999", "blocked_streak": 1}
        ts = watch.migrate_state(old)["targets"][watch.LEGACY_TARGET_KEY]
        self.assertEqual((ts["status"], ts["blocked_streak"], ts["good"]),
                         ("in_stock", 1, True))
        self.assertNotIn("price", ts)  # old string price would break formatting

    def test_empty_state(self):
        self.assertEqual(watch.migrate_state({}), {"targets": {}})

    def test_current_format_untouched(self):
        state = {"targets": {"a": {"status": "x"}}}
        self.assertIs(watch.migrate_state(state), state)


def make_target(outcome, key="t", site="Amazon"):
    """A target whose check returns (or raises) `outcome`."""
    def check():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return watch.Target(key=key, site=site, label="S25 Ultra Whitesilver 512GB", check=check)


IN_STOCK = watch.Result("in_stock", good=True, status_text="in stock",
                        title="S25", price=109999, url=URL)
NOT_LISTED = watch.Result("not_listed", good=False, status_text="not listed yet")


class Alerts(unittest.TestCase):
    """run() should alert once per event, and at the right threshold."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch, "STATE_FILE", Path(tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = argparse.Namespace(only=None, heartbeat=False)

    def run_with(self, *targets, times=1):
        """Run `times` checks over `targets`; collect alert titles."""
        with mock.patch.object(watch, "notify") as notify:
            self.codes = [watch.run(self.args, list(targets)) for _ in range(times)]
        return [c.args[0] for c in notify.call_args_list]

    def test_good_status_alerts_once_not_every_run(self):
        alerts = self.run_with(make_target(IN_STOCK), times=3)
        self.assertEqual(alerts, ["Amazon: S25 Ultra Whitesilver 512GB is in stock!"])

    def test_losing_good_status_alerts(self):
        self.run_with(make_target(IN_STOCK))
        sold_out = watch.Result("out_of_stock", good=False, status_text="sold out")
        self.assertEqual(self.run_with(make_target(sold_out)),
                         ["Amazon: S25 Ultra Whitesilver 512GB is now sold out"])

    def test_first_appearance_alerts_even_if_not_buyable(self):
        self.run_with(make_target(NOT_LISTED))
        listed = watch.Result("out_of_stock", good=False,
                              status_text="listed, not buyable yet")
        self.assertEqual(len(self.run_with(make_target(listed))), 1)

    def test_price_drop_to_target_alerts_once_and_rise_alerts_again(self):
        above = watch.Result("above_target", good=False, status_text="above your target",
                             price=139999)
        hit = watch.Result("at_target", good=True, status_text="at ₹1,05,999",
                           price=105999)
        self.assertEqual(self.run_with(make_target(above, site="Flipkart"), times=2), [])
        self.assertEqual(self.run_with(make_target(hit, site="Flipkart"), times=2),
                         ["Flipkart: S25 Ultra Whitesilver 512GB is at ₹1,05,999!"])
        self.assertEqual(self.run_with(make_target(above, site="Flipkart")),
                         ["Flipkart: S25 Ultra Whitesilver 512GB is now above your target"])

    def test_ordinary_blocks_alert_once_at_threshold(self):
        alerts = self.run_with(make_target(watch.Blocked("CAPTCHA")),
                               times=watch.BLOCKED_ALERT_AFTER + 3)
        self.assertEqual(alerts, ["Amazon watcher can't see the page"])
        self.assertEqual(set(self.codes), {2})

    def test_redesign_alerts_sooner_with_its_own_message(self):
        alerts = self.run_with(make_target(watch.LayoutChanged("no title")),
                               times=watch.LAYOUT_ALERT_AFTER)
        self.assertEqual(alerts, ["Amazon watcher needs updating"])

    def test_single_odd_page_does_not_alert(self):
        self.assertEqual(self.run_with(make_target(watch.LayoutChanged("no title"))), [])

    def test_targets_keep_separate_state(self):
        # One target being blocked must not affect the other.
        alerts = self.run_with(make_target(watch.Blocked("CAPTCHA"), key="a"),
                               make_target(IN_STOCK, key="b", site="Flipkart"), times=2)
        self.assertEqual(alerts, ["Flipkart: S25 Ultra Whitesilver 512GB is in stock!"])
        state = watch.load_state()["targets"]
        self.assertEqual((state["a"]["blocked_streak"], state["b"]["blocked_streak"]), (2, 0))

    def test_only_filters_targets(self):
        self.args.only = ["b"]
        alerts = self.run_with(make_target(IN_STOCK, key="a"),
                               make_target(IN_STOCK, key="b", site="Flipkart"))
        self.assertEqual(alerts, ["Flipkart: S25 Ultra Whitesilver 512GB is in stock!"])

    def test_heartbeat_summarises_each_target_and_resets_counters(self):
        target = make_target(NOT_LISTED)
        self.run_with(target, times=2)
        self.run_with(make_target(watch.Blocked("CAPTCHA")))
        self.args.heartbeat = True
        with mock.patch.object(watch, "notify_ntfy") as push:
            watch.run(self.args, [target])
        body = push.call_args.args[1]
        self.assertIn("not listed yet. 3 checks ok, 1 blocked", body)
        self.assertEqual(push.call_args.kwargs["priority"], "low")
        ts = watch.load_state()["targets"]["t"]
        self.assertEqual((ts["ok_since_heartbeat"], ts["blocked_since_heartbeat"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
