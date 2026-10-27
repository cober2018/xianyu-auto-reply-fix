import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from utils.item_publisher import ItemPublisher


def _request(**overrides):
    values = {
        "price": 5.9,
        "originalPrice": None,
        "title": None,
        "description": None,
        "images": [{"url": "https://evil.example/from-request.jpg"}],
        "categoryId": None,
        "catId": None,
        "catName": None,
        "channelCatId": None,
        "tbCatId": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _item(**overrides):
    item = {
        "title": "数字货币量化投资实战教程 Python 量化实操风控 SOP",
        "desc": "描述正文",
        "images": ["https://should-not-use.example/images.jpg"],
        "imageInfos": [
            {"url": "https://img.example/real.jpg", "widthSize": 1200, "heightSize": 900},
        ],
        "itemCatDTO": {
            "catId": "50023914",
            "catName": "电子资料",
            "channelCatId": "ch-50023914",
        },
        "itemPriceDTO": {
            "priceInCent": "190",
            "origPriceInCent": "100",
            "noBargain": "true",
        },
        "itemLabelExtList": [{"labelType": "common", "text": "原标签", "from": "itemDO"}],
        "itemAddrDTO": {"divisionId": 320100, "gps": "118.1,31.2", "poiName": "原地址"},
        "itemPostFeeDTO": {
            "canFreeShipping": True,
            "supportFreight": True,
            "onlyTakeSelf": False,
        },
        "quantity": 4,
        "itemTextDTO": {
            "title": "数字货币量化投资实战教程 Python 量化实操风控 SOP",
            "desc": "描述正文",
            "titleDescSeparate": False,
        },
    }
    item.update(overrides)
    return item


class ItemPriceEditTests(unittest.TestCase):
    def _publisher(self):
        return ItemPublisher("unb=1; _m_h5_tk=tok_1", "acct")

    def _fields(self, **overrides):
        fields = {
            "title": "直通课程",
            "description": "描述正文",
            "price": 1.9,
            "originalPrice": 1.0,
            "images": [{"url": "https://img.example/real.jpg", "widthSize": 800, "heightSize": 600}],
            "catId": "50023914",
            "catName": "电子资料",
            "channelCatId": "ch-50023914",
            "quantity": 4,
            "itemLabelExtList": [{"labelType": "common", "text": "原标签", "from": "itemDO"}],
            "itemAddrDTO": {"divisionId": 320100, "gps": "118.1,31.2", "poiName": "原地址"},
            "itemPostFeeDTO": {
                "canFreeShipping": True,
                "supportFreight": True,
                "onlyTakeSelf": False,
            },
            "itemTextDTO": {
                "title": "直通课程",
                "desc": "描述正文",
                "titleDescSeparate": False,
            },
            "itemPriceDTO": {"priceInCent": "190", "origPriceInCent": "100", "noBargain": "true"},
            "deliveryChoice": "无需邮寄",
        }
        fields.update(overrides)
        return fields

    def _capture(self, publisher, response=None):
        captured = {}

        async def capture(**kwargs):
            captured.update(kwargs)
            return response or {"ret": ["SUCCESS::调用成功"]}

        publisher._post_mtop = capture
        return captured

    def test_missing_title_image_or_category_does_not_call_platform(self):
        publisher = self._publisher()

        async def fail_if_posted(*_args, **_kwargs):
            raise AssertionError("缺字段时不应请求平台")

        publisher._post_mtop = fail_if_posted
        publisher.get_public_channel = fail_if_posted
        cases = [
            self._fields(title=""),
            self._fields(images=[]),
            self._fields(images=["  "]),
            self._fields(catId="", categoryId="", channelCatId=""),
            self._fields(),
        ]
        prices = [5.9, 5.9, 5.9, 5.9, 0]
        for fields, price in zip(cases, prices):
            with self.assertRaises(ValueError) as caught:
                asyncio.run(publisher.edit_item_price(item_id="1092536418156", item_fields=fields, new_price=price))
            self.assertIn("修改在售价格", str(caught.exception))

    def test_price_payload_keeps_original_fields_and_lifts_strikethrough(self):
        publisher = self._publisher()
        captured = self._capture(publisher)

        async def fail_if_channel(*_args, **_kwargs):
            raise AssertionError("改价不应重新拉取发布标签")

        publisher.get_public_channel = fail_if_channel
        asyncio.run(
            publisher.edit_item_price(
                item_id="1092536418156",
                item_fields=self._fields(),
                new_price=5.9,
                original_price=1.0,
            )
        )

        payload = captured["payload"]
        self.assertEqual(captured["api_name"], "mtop.idle.pc.idleitem.edit")
        self.assertEqual(payload["itemId"], "1092536418156")
        self.assertEqual(payload["itemPriceDTO"]["priceInCent"], "590")
        self.assertEqual(payload["itemPriceDTO"]["origPriceInCent"], "590")
        self.assertEqual(payload["itemPriceDTO"]["noBargain"], "true")
        self.assertEqual(payload["itemTextDTO"]["title"], "直通课程")
        self.assertFalse(payload["itemTextDTO"]["titleDescSeparate"])
        self.assertEqual(payload["quantity"], "4")
        self.assertEqual(payload["itemLabelExtList"], [{"labelType": "common", "text": "原标签", "from": "itemDO"}])
        self.assertEqual(payload["itemAddrDTO"]["divisionId"], 320100)
        self.assertIsInstance(payload["itemAddrDTO"]["divisionId"], int)
        self.assertTrue(payload["itemPostFeeDTO"]["canFreeShipping"])
        self.assertTrue(payload["itemPostFeeDTO"]["supportFreight"])
        self.assertEqual(payload["sourceId"], "pcItemEdit")
        self.assertEqual(payload["bizcode"], "pcItemEdit")
        self.assertEqual(payload["publishScene"], "pcItemEdit")
        self.assertNotIn("tbCatId", payload["itemCatDTO"])
        self.assertNotIn("pcMainPublish", str(payload))
        self.assertTrue(str(payload["uniqueCode"]).isdigit())

    def test_higher_strikethrough_stays_above_sale_price(self):
        publisher = self._publisher()
        captured = self._capture(publisher)
        asyncio.run(
            publisher.edit_item_price(
                item_id="1092536418156",
                item_fields=self._fields(originalPrice=9.9),
                new_price=5.9,
                original_price=9.9,
            )
        )
        self.assertEqual(captured["payload"]["itemPriceDTO"]["priceInCent"], "590")
        self.assertEqual(captured["payload"]["itemPriceDTO"]["origPriceInCent"], "990")

    def test_missing_freight_can_still_apply_explicit_delivery_choice(self):
        publisher = self._publisher()
        payload = publisher._build_price_edit_payload(
            item_id="1092536418156",
            item_fields=self._fields(itemPostFeeDTO={}, deliveryChoice="包邮"),
            new_price=5.9,
        )
        self.assertTrue(payload["itemPostFeeDTO"]["canFreeShipping"])
        self.assertTrue(payload["itemPostFeeDTO"]["supportFreight"])

    def test_fetch_item_detail_reads_platform_itemdo(self):
        publisher = self._publisher()
        captured = self._capture(
            publisher,
            {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemId": "1092536418156", "title": "标题"}}},
        )
        item = asyncio.run(publisher.fetch_item_detail("1092536418156"))
        self.assertEqual(item["title"], "标题")
        self.assertEqual(captured["api_name"], "mtop.taobao.idle.pc.detail")
        self.assertEqual(captured["version"], "1.0")
        self.assertEqual(captured["payload"], {"itemId": "1092536418156"})

        async def fail_if_posted(*_args, **_kwargs):
            raise AssertionError("空商品 ID 不应请求平台")

        publisher._post_mtop = fail_if_posted
        with self.assertRaises(ValueError):
            asyncio.run(publisher.fetch_item_detail("  "))

    def test_quantity_edit_uses_item_edit_scene(self):
        publisher = self._publisher()
        captured = self._capture(publisher)
        asyncio.run(
            publisher.edit_item_quantity(
                item_id="1092536418156",
                item_fields={
                    "title": "标题",
                    "description": "描述",
                    "price": 1.9,
                    "images": ["https://img.example/real.jpg"],
                    "categoryId": "50023914",
                },
                quantity=7,
            )
        )
        payload = captured["payload"]
        self.assertEqual(payload["quantity"], "7")
        self.assertEqual(payload["itemId"], "1092536418156")
        self.assertEqual(payload["itemPriceDTO"]["priceInCent"], "190")
        self.assertEqual(payload["sourceId"], "pcItemEdit")
        self.assertEqual(payload["bizcode"], "pcItemEdit")
        self.assertEqual(payload["publishScene"], "pcItemEdit")


class CategoryFallbackTests(unittest.TestCase):
    def _channel(self):
        return {
            "data": {
                "categoryPredictResult": {
                    "catId": "",
                    "catName": "",
                    "channelCatId": "",
                    "tbCatId": "",
                },
                "cardList": [
                    {
                        "cardData": {
                            "propertyName": "分类",
                            "valuesList": [
                                {
                                    "catName": "电子资料",
                                    "channelCatId": "ch-50023914",
                                    "isClicked": True,
                                    "score": 1,
                                }
                            ],
                        }
                    },
                    {
                        "cardData": {
                            "propertyName": "类目",
                            "valuesList": [
                                {
                                    "catName": "电子资料",
                                    "catId": "50023914",
                                    "transportData": {"tbCatId": "tb-keep"},
                                },
                                {
                                    "catName": "其他",
                                    "catId": "9",
                                    "tbCatId": "tb-other",
                                    "score": 100,
                                },
                            ],
                        }
                    },
                ],
            }
        }

    def test_split_cards_merge_by_name_and_prefer_known_category(self):
        result = ItemPublisher._validate_category_recommendation(self._channel())
        self.assertEqual(result["catId"], "50023914")
        self.assertEqual(result["catName"], "电子资料")
        self.assertEqual(result["channelCatId"], "ch-50023914")
        self.assertEqual(result["tbCatId"], "tb-keep")

    def test_tb_cat_id_from_another_category_name_is_not_reused(self):
        channel = {
            "data": {
                "categoryPredictResult": {
                    "catId": "50023914",
                    "catName": "电子资料",
                    "channelCatId": "ch-50023914",
                    "tbCatId": "",
                },
                "cardList": [
                    {
                        "cardData": {
                            "propertyName": "类目",
                            "valuesList": [{"catName": "其他", "catId": "9", "tbCatId": "tb-other"}],
                        }
                    }
                ],
            }
        }
        with mock.patch("utils.item_publisher.logger.info") as info:
            result = ItemPublisher._validate_category_recommendation(channel)
        self.assertEqual(result["tbCatId"], "")
        self.assertIn("tbCatId", info.call_args[0][0])

    def test_missing_required_category_names_only_required_fields(self):
        channel = {
            "data": {
                "categoryPredictResult": {"catName": "只有名字"},
                "cardList": [],
            }
        }
        with self.assertRaises(ValueError) as caught:
            ItemPublisher._validate_category_recommendation(channel)
        message = str(caught.exception)
        self.assertIn("catId", message)
        self.assertIn("channelCatId", message)
        self.assertNotIn("tbCatId", message)


class PriceEditFieldTests(unittest.TestCase):
    def test_fields_come_from_platform_detail_not_local_images(self):
        from reply_server import _build_price_edit_fields, _item_content_snapshot

        fields = _build_price_edit_fields(_item(), _request())
        self.assertEqual(fields["images"][0]["url"], "https://img.example/real.jpg")
        self.assertEqual(fields["images"][0]["widthSize"], 1200)
        self.assertNotIn("should-not-use", str(fields["images"]))
        self.assertNotIn("evil.example", str(fields["images"]))
        self.assertEqual(fields["originalPrice"], 1.0)
        self.assertEqual(fields["price"], 1.9)
        self.assertEqual(fields["quantity"], 4)
        self.assertEqual(fields["itemLabelExtList"][0]["text"], "原标签")
        self.assertEqual(fields["itemAddrDTO"]["divisionId"], 320100)
        self.assertTrue(fields["itemPostFeeDTO"]["canFreeShipping"])
        self.assertNotIn("tbCatId", fields)

        without_size = _item(imageInfos=[{"url": "https://img.example/real.jpg"}])
        sized = _build_price_edit_fields(without_size, _request())
        self.assertEqual(sized["images"][0]["widthSize"], 1000)
        self.assertEqual(sized["images"][0]["heightSize"], 1000)

        snapshot = _item_content_snapshot(_item())
        changed_price = _item()
        changed_price["itemPriceDTO"] = {"priceInCent": "590", "origPriceInCent": "590"}
        self.assertEqual(_item_content_snapshot(changed_price), snapshot)

    def test_missing_platform_images_or_category_raise_before_edit(self):
        from reply_server import _build_price_edit_fields

        with self.assertRaises(ValueError) as missing_images:
            _build_price_edit_fields(_item(imageInfos=[], imageInfoDOList=[]), _request())
        self.assertIn("images", str(missing_images.exception))

        with self.assertRaises(ValueError) as missing_category:
            _build_price_edit_fields(_item(itemCatDTO={}), _request())
        self.assertIn("catId", str(missing_category.exception))

        fallback = _build_price_edit_fields(
            _item(imageInfos=[], imageInfoDOList=[{"url": "https://img.example/echo.jpg", "width": 640}]),
            _request(),
        )
        self.assertEqual(fallback["images"][0]["url"], "https://img.example/echo.jpg")
        self.assertEqual(fallback["images"][0]["widthSize"], 640)

    def test_price_route_is_in_openapi(self):
        import reply_server

        schema = reply_server.app.openapi()
        operation = schema["paths"]["/items/{cookie_id}/{item_id}/price"]["post"]
        self.assertIn("price", operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].lower() + str(operation))
