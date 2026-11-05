# 录入与续跑

工具输入由 Agent 根据原始页面整理，运营无需写 JSON。工作目录内的图片、原始截图用相对路径，不复制登录凭据、收件地址或订单信息。以下只是格式示意，不能作为商品、价格或确认的证据。

候选：一个 JSON 数组，例：
```json
[{"product":"控油洁面","role":"own","brand":"品牌名","title":"页面完整商品名","sku":"120g 单支","url":"https://brand.example/product/123","platform":"品牌官网","shop":"品牌官网","basis":"品名、容量来自官网同一款","image":"收件箱/参考图.png"}]
```
平台名须与链接域名一致。候选可来自官网；同一款不同渠道继续用同一已选 ID 留证。没有图先不填 image，不能用生成图冒充商品图。批次工作目录之外的文件先复制到该批次的收件箱。对登录受限网站不导出账号或复用浏览器 cookie。

观察（JSON 数组）：必填 candidate_id, platform, shop, kind, scope, observed_at（含时区）, url, excerpt, assets, original（布尔）, fields。kind 为 campaign / current / offer / rule；scope 为 product_page / brand_page / rule_page / checkout / live。使用 observe 前确认 JSON 中 URL、截图、条件和页面规格属于同一条事实。original_assets、screenshot_assets 为 assets 的子集，原图和页面截图不能用相同文件冒充。

- 活动图：original=true，给两种附件，fields 包含 页面规格、活动期（含年和双十一）、图中文字。常规图：另给 双十一字样=无 和 版本依据（当前页面、包装或版本的定位信息）。短边 600px 是本工具拒绝明显小图的底线，仍需审看清晰度。
- 优惠：一条仅一种价格口径（日常价、活动标价、宣传到手价、结算实见价）；页面规格与候选 sku 完全一致。补资料包含活动期 / 日常价依据 / 结算条件及独立截图。赠品不折现金，门槛、会员条件和时间作为字段保留。无可靠数据可只记录 gap。
- 规则：只记录明确活动期及官方原文，附实际规则截图/文本；不推定特定 SKU 参与。重新访问失败只剩索引线索时不当已验事实。
- 实际生成的 JSON Schema 可随时用 `scripts/run flow schema` 查看。已撤销的观察用 `withdraw E-ID --reason '原因'`，已补缺口用 `gap --resolve '原原因'`。输出回执给出本次导出目录。

重复同一输入应返回同一候选/证据编号；批次内再次 export 可复用相同导出。批次内的 state.json、留证 与 inputs 应一起保留；不直接修改 导出回执.json。

## 四项逐页核对

对**允许读取的公开商品页**，用 `scripts/run channel collect --run "$RUN" --candidate "$CID" --variant "$VARIANT" --url "$ITEM_URL" --out "$RUN/渠道采集"`。款名取已认候选的可区分片段，例如“绿泥”或“炭爽抗黑头”。使用真实详情或抖音商品卡公开分享链接。此入口只读公开 HTML，不读取浏览器登录态，不能假定动态加载、登录专属或 App 内的优惠可采。已被宿主明确限制的商品保存于批次的 `受限商品.json`（items 为平台与商品编号数组），请求前和发生跳转时均检查。

采集回执分别保存所选规格、主图、活动标价和逐条优惠。范围价、券后宣传价、未标件数、推荐商品、本期活动年份不明、没有写明的优惠均不得补猜。要在允许查看的独立页面上选同一款、同一规格，展开完整优惠，在相近时间留带主图与证据的截图。由运营或 Agent 独立阅读；只有一张用户提交的截图不能证明程序采到了该页。

把独立记录写成 JSON，字段为 `source: rendered_page`、`page_url`、`page_title`、`at`（含时区）、`selected_sku`、`activity_price`（金额字符串）、`campaign`（页面活动原文）、`benefits`（逐条原文）、`screenshot`（记录文件旁已去除个人信息的图片）、`main_image_box`（截图内主图左、上、右、下像素）。没有对应证据的字段写 null；规格和促销不得从采集回执粘贴作为独立证据。分别在两个平台运行 `scripts/run channel compare --capture "$CAPTURE" --reference "$REFERENCE" --out "$RESULT"`：true 为相符，false 为不符，null 为缺证或规格不对应。商品编号和时间必须可对应，超出 10 分钟作废。四项齐全才可按本页通过；回执不自动写入正式报告。正式归档仍遵守上面的活动图、更新图与结算价格条件。
