跨境流域临灾数据融合与预警复盘系统

河段与通知：Repository.register_reach 登记有传播时间的下游关联，
assign_station 关联观测站与河段，issue_notice 把通知关联到当时的观测版本和河段。
update_notice 只能将待发通知送达或取消、送达后确认；已送达/确认事实不可撤销
或改派。reach_notices 可按河段追溯。风险分析计算站点聚合时窗。

证据可信度到处置：event_watermark 汇总各站可靠度（新鲜度×质量），输出
confidence 与 evidence（trusted/insufficient/link_failed）。观测全部过期或质量
为零时 confidence 归零、evidence 为 link_failed；无观测或可用覆盖不足为
insufficient。此时风险等级按保守原则保留——既不当作新的可信证据，也不当作
低风险；decide 不会批准（approve），仅保留处置或转人工复核（hold/review），
告警意见（alert_assessment/Application.assess）明确标注无可靠观测。
站点恢复或迟到更正后按同一套依据重评；仍绑定旧观测版本的待发通知被取消，
可依据新版本重新签发，已送达/确认事实留存。重复上报、重放与服务重启幂等：
同一观测内容不重复建版，同一依据的处置不重复落库，决定与状态推进同事务写入。

运行验收：
python3 -m basin_warning run --fixture fixtures/acceptance.json --database /tmp/basin_warning.sqlite3 --output /tmp/basin_warning-output

核验：
python3 -m basin_warning verify --database /tmp/basin_warning.sqlite3 --output /tmp/basin_warning-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.fusion.test_conflict_resolution -v

构建检查：
python3 -m compileall -q basin_warning tests
