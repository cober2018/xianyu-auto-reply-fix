# listing-pipeline — 闲鱼上架流水线(网盘入库之后的部分)

接在 `../netdisk-mcp/` 之后:入库拿到自己的 `share_url` 后,出图、写文案、上架、绑自动发货。

## 完整流程(OpenClaw 编排时按这个顺序)

```
1. netdisk-mcp ingest_share_link  → share_url / passcode / dest_path
2. python3 cover.py --title <商品标题> --out build/<slug>_cover.jpg
3. 按 prompts/listing.md 的提示词生成 title/description(或人工改模板)
4. 人工确认:标题 / 价格 / 描述 / 封面 / 卡券内容 五样一起看
5. POST /cards     (generate_delivery_rule=true, 自动绑发货规则)
6. POST /product-publish   (images=[封面 base64])
7. 上架后核对:GET /items/cookie/{cookie_id} 里出现新商品
```

第 4 步不能省:发布不可撤销,且闲鱼对虚拟商品限流严格,连发要间隔。

## 文件

- `cover.py` — 固定模板封面(3:4,1080x1440)。改风格只改 `TEMPLATE`。
- `prompts/listing.md` — 文案提示词 + 卡券内容模板。LLM 输出必须 JSON。
- `build/` — 产物(封面图、listing JSON)。凭证和链接不要提交 git。

## 对接的闲鱼服务接口(本机 http://127.0.0.1:8090)

| 步骤 | 接口 |
|---|---|
| 登录 | `POST /login {username,password}` → token,Bearer 头 |
| 建卡券+发货规则 | `POST /cards {name,type:"text",text_content,generate_delivery_rule:true}` |
| 上架 | `POST /product-publish {account_id,title,description,price,images:[base64|URL],delivery_method:"包邮"}` |
| 核对在售 | `GET /items/cookie/{cookie_id}` |

发货规则按**卡券名称**匹配商品标题,所以卡券名用短关键词(如「费曼学习法」),不要用完整商品标题。
