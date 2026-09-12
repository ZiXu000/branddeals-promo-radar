ILANG
TYPE: agents | PROJECT: branddeals-promo-radar | LANG: zh

::STATE{@PROJECT, what:消费品牌官方优惠与优惠码自动站, repo:branddeals-promo-radar, deploy:Cloudflare Pages, runtime:GitHub Actions}
::STATE{@PIPELINE, step1:scraper.py 抓公开源→data/offers.json, step2:build.py 渲染→site/, step3:workflow 定时跑并 commit}

::MODULE{WHAT|title:这个项目是什么}
  一个零服务器 零密钥 零运行时推理的静态优惠站。
  数据来自各品牌自己公开的产品 feed 与官方页面 每隔几小时由 GitHub Actions 重抓一次。
  站点每次重建都提交回仓库 所以仓库的 commit 历史就是它的活跃度记录。

::MODULE{BOUNDARY|title:边界 什么绝对不许做}
  ::BOUNDARY{never:编优惠 编价格 编有效期 编佣金|scope:permanent}
  ::BOUNDARY{never:抓登录后的内容 不绕反爬 不违反 robots.txt|scope:permanent}
  ::BOUNDARY{never:品牌词竞价 cookie 注入 自买自推|scope:permanent}
  ::BOUNDARY{never:把 I-Lang 只当注释贴上去应付 必须被代码读 或被 AI 用|scope:permanent}

::MODULE{CONFIG|title:配置的唯一真源}
  厂商清单 抓取入口 要提取的字段 全部写在 .ilang/site.ilang。
  scraper.py 和 build.py 必须真的读它。
  判据: 改 site.ilang 里一家厂商 重跑一次 站上就该变。变不了就是做成了摆设 重做。

::MODULE{ACTIONS|title:允许的动作}
  1. 加厂商: 在 site.ilang 的 PROVIDERS 里加一行 name|homepage|source|adapter|affiliate
  2. 换抓取源: 改那一行的 source
  3. 加字段: 在 FIELDS 里加 并在 scraper 对应 adapter 里补提取逻辑
  4. 改渲染: 改 templates/ 下的模板
  5. 改频率: 改 .github/workflows/update.yml 的 cron
  以上动作都不需要动厂商清单的硬编码 因为本来就没有硬编码。

::MODULE{ADAPTERS|title:现有的抓取适配器}
  shopify_sale | 抓 /products.json 有划线价的算 deal 其余算在售商品
  jsonld_offers | 抓官方页 解 JSON-LD 里的 Product/Offer
  新增适配器: 在 scraper.py 的 ADAPTERS 字典里注册 并在 site.ilang 对应行写名字

::MODULE{VERIFY|title:改完怎么自测}
  1. python scraper.py  看每家的 items/deals 计数是否合理
  2. python build.py    看 site/ 是否生成 index/provider/deal/compare
  3. 抽一个详情页 把 JSON-LD 贴进谷歌富媒体测试工具 不该有报错
  4. 改 site.ilang 一家厂商 重跑 看站是否真的变

::FACT{key:protocol|value:I-Lang|conf:confirmed}
::LESSON{id:ilang_note|type:meta|scope:global|conf=confirmed}
  这份文件用 I-Lang 协议写 供接手本仓库的 AI 阅读 边界优先于功能 先看清 never 再动手
