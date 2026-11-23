# v1 验证结果

对应 `docs/design.zh-CN.md` 里的待验证项(V1 到 V9)和阶段(P0 到 P5)。每条结论后面写明怎么测的。没有测过的不写成结论。
环境:同 v0,us-west-2,Keycloak 26.8.2,用户 alice 和 bob。脚本在 `v1/`。

## V1 两条令牌路径的身份是否对得上:**对不上**

`v1/p0_v1_identity_paths.py`。

| 授予方式 | 读取方式 | 结果 |
|---|---|---|
| alice 用 `GetWorkloadAccessTokenForJWT`(用户 JWT) | 同一条路径 | 有令牌 |
| 同上 | `GetWorkloadAccessTokenForUserId`,用户标识分别试了 `sub`、`iss\|sub`、`iss#sub`、`iss:sub`、`iss/sub` | 都没有令牌,返回新的授权链接 |
| bob 用 `GetWorkloadAccessTokenForUserId`(`sub`) | 同一条路径 | 有令牌 |
| 同上 | `GetWorkloadAccessTokenForJWT`(bob 的 JWT) | 没有令牌 |

结论:两条路径写入的是不同的 Vault 条目。只能说这五种写法没命中,不能证明不存在对应的字符串。
影响:自动任务用 `runtimeUserId`(用户标识路径),所以授予也必须走用户标识路径。

## V2 自动任务 runtime:**成立**,但授予方式要改

`v1/auto_agent/main.py`(部署在 IAM 入站的 Runtime 上),`v1/p0_v2_runtime_grant.py`,`v1/p0_v2_iam.py`。

- 带 `runtimeUserId` 调用时,Runtime 把工作负载令牌放在 `WorkloadAccessToken` 头里交给 agent。不带 `runtimeUserId` 时没有这个头。
- 用户标识本身不会作为请求头传给 agent。agent 拿不到"这次是替谁运行"这个值,只能由调用方放进请求体。
- **托管的工作负载身份不能被调用方取令牌**:对 Runtime 的工作负载身份调用 `GetWorkloadAccessTokenForUserId` 返回 `WorkloadIdentity is linked to a service and cannot retrieve an access token by the caller`。所以授权控制台不能自己取令牌去发起授权,授予必须在 Runtime 里完成:控制台带 `runtimeUserId` 调用 Runtime 的 `connect` 模式拿授权链接,用户同意后再调用 `complete` 模式绑定会话。
- Vault 条目按工作负载身份分开:bob 在另一个工作负载身份下授予过,同一个 `sub` 通过 Runtime 读取时没有令牌。
- 会话绑定会核对用户:拿 alice 的授权会话,用 bob 的用户标识调用 `CompleteResourceTokenAuth`,返回 `AccessDeniedException: Invalid or expired session`。同一个会话随后用 alice 的标识完成成功。
- 用户标识用 `https://<issuer>/...#<sub>` 这种带符号的字符串,作为 `runtimeUserId` 和 `userId` 都被接受。
- 授权完成后,经 Runtime 读取 Vault、调用下游,下游看到 alice(`sub`、`azp=agent-bg`、`aud=itsm-api`,没有 `act`)。
- IAM:带 `runtimeUserId` 调用必须同时有 `InvokeAgentRuntime` 和 `InvokeAgentRuntimeForUser`,只给前者时返回明确的拒绝信息。资源写成 Runtime 的 ARN 和 `.../runtime-endpoint/*` 即可。不带 `runtimeUserId` 时只需要前者,但拿不到工作负载令牌,读不了 Vault。

没有测:同一个 Runtime 上用 IAM 区分 `connect`、`complete`、`use` 三种模式。这三种模式共用一个工作负载身份,IAM 只能控制能不能调用这个 Runtime,控制不了调用哪种模式,模式的区分只能写在 agent 代码里。

## V3 CloudTrail 是否记录用户标识:**不记录**

`v1/setup_trail.sh` 建了只记录 `AWS::BedrockAgentCore::Runtime` 数据事件的 trail。

- 事件 `InvokeAgentRuntime` 有调用方角色、Runtime 和端点的 ARN、`runtimeSessionId`。
- `requestParameters` 是空,没有 `runtimeUserId`。我在事件里按用户 `sub` 搜过,没有出现。
- 所以 launcher 把用户标识的哈希放进 `runtimeSessionId`,并在登记表写一条运行记录(会话 ID、用户、任务、时间),事后用会话 ID 关联 CloudTrail 和登记表。

## P1 无人值守路径:**成立**

`v1/p1_launcher.py`:EventBridge Scheduler 一次性任务 → launcher Lambda → Runtime → Vault → 下游。

| 场景 | 结果 |
|---|---|
| 用户授权后的定时触发 | 下游返回 200,任务记录 `ok` |
| 从没授权过的用户(bob)的任务 | Runtime 返回 `needs-reauth`,launcher 把授权状态置为 `needs-reauth` |
| 越过身份提供商的会话上限(300 秒)之后 | Vault 里没有令牌,`needs-reauth`;再次触发时 launcher 直接跳过,不调用 Runtime |
| 授权被关闭(`disabled`) | launcher 跳过,不调用 Runtime |

## P2 后台网关:**成立**

`v1/deploy_downstream_api.sh`、`v1/deploy_bg_gateway.sh`、`v1/attach_policy.py`、`v1/p2_gateway.py`、`v1/p2_vault_freshness.py`、`v1/gw_authorizer.py`、`v1/gw_direct.py`。

搭建:
- 下游 HTTP API(API Gateway,JWT 授权器校验签发方和受众 `itsm-api`),后面接下游 Lambda。Lambda 新增写操作 `POST /incidents/{id}/approve`,给策略当禁止对象。不带令牌返回 401。这是 V8 的退路(令牌门控的公网 HTTP API),网关到私有下游的连通方式没有测。
- Keycloak 新增交换客户端 `agent-bg-ex`(开启标准令牌交换),`agent-bg` 的令牌里加了 `agent-bg-ex` 作为受众。
- 凭据提供者(OBO,用 `agent-bg-ex`),后台网关(入站校验受众,自定义声明规则按 `azp` 匹配 `agent-bg`),网关目标(OpenAPI,两个操作 `listMyIncidents`、`approveIncident`)。
- 自动任务 runtime 的 `gateway` 模式:用从 Vault 取到的令牌作为入站令牌,调用网关的 `tools/list` 和 `tools/call`。

结果:
- 完整链路成立:Vault 令牌(`azp=agent-bg`,`aud` 含 `itsm-api` 和 `agent-bg-ex`)通过网关入站校验,网关用 `agent-bg-ex` 做 OBO 交换,下游看到 alice,`azp=agent-bg-ex`,`aud=itsm-api`,没有 `act`。交互式路径下游看到的是 `azp=agent-app`,所以下游能区分后台调用。
- `azp` 规则生效:用另一个客户端(`user-cli`)签发的令牌,受众相同、寿命相同,打开规则后被拒绝,关闭规则时同一令牌通过。
- Cedar 策略(只写一条 permit:`azp` 等于 `agent-bg` 时允许 `itsm___listMyIncidents`,其余默认拒绝)。LOG_ONLY 下两个工具都能调用。ENFORCE 下 `tools/list` 里只剩 `listMyIncidents`,调用 `approveIncident` 返回 `Tool Execution Denied: ... denied by default`,调用 `listMyIncidents` 成功。策略里 `azp` 不匹配时被拒这一条没有单独测(入站的 `azp` 规则已经先拦住了)。
- 策略动作名只有 `<目标>___<操作>`。把 `tools/list`、`initialize` 写进去,创建策略失败(`Target 'tools/list' does not exist`)。网关自己的角色要有 `GetPolicyEngine`、`AuthorizeAction`、`PartiallyAuthorizeActions`,缺了挂载策略引擎时报错。

约束:
- 入站校验失败时,网关统一返回 `403`,错误是 `insufficient_scope`,受众不匹配时也是这个错误,不能靠它判断是哪条规则拒绝的。受众是"任一匹配"。
- **网关入站拒绝剩余有效期不足约一分钟的令牌。** 用同一个用户的令牌对比:寿命 60 秒时刚签发就被拒;寿命 120 秒时,剩余 120 秒和 79 秒通过,剩余 48 秒和 28 秒被拒。精确阈值在 48 到 79 秒之间。
- 所以后台客户端的访问令牌寿命要大于一分钟,v0 的 60 秒不能用,改成了 120 秒。撤销后已签发令牌仍可用的窗口因此至少是两分钟。前面几次 Vault 令牌被拒,就是这个原因:改成 120 秒后同样的调用通过(其间也调整过受众和 `azp` 规则,最终配置不变)。
- Vault 在令牌剩余约一分钟时刷新(`p2_vault_freshness.py`:连续 150 秒、每 8 秒左右一次调用,同一个令牌一直用到剩余约 60 秒,然后换新令牌),两个阈值对得上,全部调用都是 200。
- 靠近身份提供商会话上限的最后一分钟,令牌寿命被会话截短(观察到剩余 29、21、13、4 秒的令牌),全部被网关拒绝。所以登记表里的 `expires_at` 要比身份提供商的上限早一分钟以上。

## V7 撤销的实际语义:**有效,有一分钟左右的窗口**

`v1/p3_revoke.py`:授权 alice,用一次调用让 Vault 里有缓存令牌,在身份提供商撤销,然后每几秒调用一次无人值守路径(Vault 读取加下游调用)。

| 撤销方式 | 撤销后的表现 |
|---|---|
| 结束用户会话(`POST /users/{id}/logout`) | 约 35 秒内仍返回缓存的访问令牌,下游返回 200。令牌剩余不足约一分钟之后,Vault 刷新失败,返回 `needs-reauth`(45 秒时) |
| 删除用户对后台客户端的同意(`DELETE /users/{id}/consents/agent-bg`) | 同上:34 秒时令牌剩余 67 秒仍有效,44 秒时 `needs-reauth` |

- 两种方式刷新都会失败,Vault 不会保留另一份还能用的凭据。撤销后再次授权可以正常完成。
- 窗口由 Vault 的缓存决定:它一直返回缓存令牌,直到令牌剩余约一分钟才去刷新。访问令牌寿命 120 秒时,令牌签发后约 60 秒内都可能被返回,所以撤销之后经 Vault 取令牌最多再用约一分钟。
- 已经发给下游的令牌不受影响,下游自己校验签名和 `exp`,不会向身份提供商确认,最长可用到 `exp`(120 秒)。agent 如果在一次任务里一直拿着同一个令牌,窗口就是这么长。
- 每种方式只测了一次。

## 托管组件能不能替代自建代码(文档核对,没有实测)

问题:无人值守时"取用户的令牌再调下游",有没有不用自己写代码的托管组件?我按文档核对,在我查到的范围内,没有。

| 组件 | 文档怎么说 | 结论 |
|---|---|---|
| Harness(Runtime 之上的托管封装) | 用 SigV4 调用时,harness 不会把按用户的身份传给下游工具。Token Vault 的按用户令牌存储和 OBO,只在用 Bearer JWT 走 OAuth 入站时可用 [1] | 无人值守没有用户 JWT,做不了 |
| Gateway 的 3LO 目标与同意门户 | 出站授权是替入站时已认证的用户访问目标 [2]。门户要求网关用 JWT 入站 [3] | 用户在场可以,无人值守没有 JWT |
| Gateway 的 IAM 入站 | IAM 入站认证的是 AWS 主体,文档没有说它带终端用户身份 | 推断做不了,没测 |
| Runtime 加用户标识头 | SigV4 调用带 `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`,agent 代码用工作负载令牌取用户令牌,这条路径下按用户的出站 OAuth 可用 [4][5]。文档同时说它不核对用户标识,有身份提供商的生产环境建议用 JWT [4] | 无人值守的唯一路径,需要自建代码。这就是 v1 用的 |

补充两点:
- AWS 安全博客把 3LO 描述为需要用户交互、不适合后台操作 [6]。P1 的结果是授权一次之后可以无人值守取令牌,交互只发生在授予那一次。
- 入站的 `allowedClients` 按 `client_id` 声明校验 [7]。我测到的 Keycloak 访问令牌没有这个声明,只有 `azp`;Okta 的客户端标识在 `cid` 里,也不适用 [8]。所以后台网关用 `azp` 的自定义声明规则。`allowedClients` 本身我没有测。

来源:
[1] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html
[2] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html
[3] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal-prerequisites.html
[4] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html
[5] https://aws.amazon.com/blogs/industries/automate-customer-complaint-classification-with-ai-agents-on-aws/
[6] https://aws.amazon.com/blogs/security/propagate-user-authorization-context-in-ai-agents-with-amazon-bedrock-agentcore/
[7] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html
[8] https://aws.amazon.com/blogs/machine-learning/implement-on-behalf-of-token-exchange-for-multi-tenant-agents-with-amazon-bedrock-agentcore-gateway/

## 阶段进度

| 阶段 | 状态 |
|---|---|
| P0 验证 | V1、V2、V3、V6、V7 已测完。V4 只读了配置(`agent-bg` 的空闲超时和最大寿命都是 300 秒,域的会话最长 10 小时、空闲 30 分钟,条款确认动作默认关闭),7 天上限、按组区分、条款确认都没测。V5、V9 没测,V8 只做了退路 |
| P1 自动任务路径 | 完成。"对所有主体显式拒绝"只验证了默认拒绝(没有 `InvokeAgentRuntimeForUser` 的角色被拒),没有加显式 Deny 语句 |
| P2 后台网关 | 完成,见上 |
| P3 授权控制台与登记表 | 部分。登记表、launcher、运行记录、`needs-reauth`、授权关闭时跳过都有。授权控制台页面、确认文本、撤销、`spaces list` 没做。授予用的 `connect` 和 `complete` 接口已在 Runtime 里 |
| P4 交互式 runtime 与 broker | 未开始 |
| P5 对比运行 | 未开始 |

## 测试留下的状态

- Keycloak:`agent-bg` 的访问令牌寿命是 120 秒(有意保留),`agent-bg-ex` 客户端和 `agent-bg` 上的受众映射器保留。`user-cli` 已还原(寿命 900 秒,测试映射器已删)。
- 后台网关:入站有 `azp` 规则,挂着策略引擎,模式 ENFORCE。
- AWS 里 v1 新增的资源名都带 `obo-test-v1` 或 `obo_test_v1` 前缀。v1 的清理脚本还没写。
- 开着的有:CloudTrail 数据事件 trail、自动任务 runtime、后台网关和策略引擎、HTTP API、launcher、登记表、本地 Chrome 调试端口。
