# AgentCore 委托授权 PoC

[English](README.md) | 简体中文

在 Amazon Bedrock AgentCore 上,让 agent 代替用户行动的概念验证,用户在场和不在场都覆盖:

1. **PoC 1,用户在场:OBO 令牌交换。** agent 经 AgentCore Gateway 调用下游 API,网关在每次请求时把用户令牌换成下游令牌,下游看到的是真实用户。
2. **PoC 2,用户不在场:3LO + Token Vault。** 用户同意一次,之后由调度器在没有用户会话的情况下,从 AgentCore Identity 的 Token Vault 取出该用户的令牌,硬过期时间由身份提供商控制。
3. **v1,基于 AgentCore Runtime 的无人值守路径。** EventBridge Scheduler 触发 launcher,launcher 查登记表后用 `runtimeUserId` 调用自动任务 Runtime;Runtime 从 Vault 取用户的令牌,再调用后台网关(`azp` 规则、独立的 OBO 交换客户端、Cedar 策略)。授予、使用、撤销和审计都测过。

第二个 PoC 模拟的是工作区类平台里常见的一种模式:每用户一个认证锚点、把会话转发给各个工作区、再由服务端保管一份刷新凭据,让无人值守的自动任务在硬过期之前可以运行。本仓库把这套模式映射到 AgentCore,并测出它哪里成立、哪里有缺口。

状态:实验性质,不是生产代码。许可证:MIT-0(见 `LICENSE`)。所有测试都在同一个账号、同一个区域(us-west-2)里,身份提供商是 Keycloak,多数检查只跑了一次:数字是测量值,不是基准测试。v1 的设计和结果是中文文档:`docs/design.zh-CN.md` 和 `docs/results-v1.md`。设计里有没做的部分,哪些测过见结果文件。

## v1 架构

```mermaid
flowchart TB
    subgraph usr["用户在场(一次)"]
        U["用户"]
        CON["授权入口<br/>(测试里由脚本代替)"]
    end
    IDP[("身份提供商<br/>Keycloak")]
    subgraph ctl["你账号里的控制面"]
        SCH["EventBridge Scheduler"]
        LA["launcher<br/>Lambda"]
        REG[("登记表<br/>DynamoDB")]
    end
    subgraph ac["Amazon Bedrock AgentCore"]
        RT["自动任务 Runtime<br/>IAM 入站"]
        ID["Identity<br/>Token Vault"]
        GW["后台 Gateway<br/>azp 规则 + Cedar 策略"]
    end
    D["下游 API<br/>JWT 授权器 + Lambda"]
    U --> CON
    CON -->|"① 连接、完成<br/>(带 runtimeUserId)"| RT
    U -->|"② 登录并同意"| IDP
    IDP -->|"③ 授权码"| ID
    SCH --> LA
    LA <-->|"读授权和任务"| REG
    LA -->|"④ 以用户身份调用<br/>(runtimeUserId)"| RT
    RT -->|"⑤ 取用户的令牌"| ID
    RT -->|"⑥ 令牌作为入站令牌"| GW
    GW -->|"⑦ OBO 令牌交换"| ID
    ID <-->|"刷新、交换"| IDP
    GW -->|"⑧ 只发给下游的令牌"| D
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    class RT,ID,GW agentcore
    class IDP idp
    class U,CON,SCH,LA,REG app
    class D down
    style ac fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    style usr fill:#f8fafc,stroke:#94a3b8,color:#334155
    style ctl fill:#f8fafc,stroke:#94a3b8,color:#334155
```

- **授予(用户在场,一次)。** 授权入口带 `runtimeUserId` 调用 Runtime(先连接,再完成),由 Runtime 去调 Identity。原因是调用方不能为 Runtime 的托管工作负载身份取工作负载令牌,而且 Vault 条目按工作负载身份分开。
- **使用(用户不在场)。** Scheduler、launcher(查登记表)、Runtime(读 Vault)、后台网关(`azp` 规则、用独立客户端做 OBO 交换、Cedar 默认拒绝)、下游。没有任何主体持有 `GetWorkloadAccessTokenForUserId`。
- **为什么有一段自建代码。** 只有 launcher 和 Runtime 里一小段固定的 agent。按文档,Harness、Gateway 的 3LO 目标和同意门户都要求入站带 JWT,所以用户不在场时没有托管组件能取用户的令牌。

## PoC 1 和 2 的架构

橙色 = AgentCore 的功能,橙色粗线是 OBO(分组框就是 AgentCore 的服务)。蓝色 = 外部身份提供商。绿色 = 下游。红色虚线 = 信任关键的调用方和审计缺口。◆ = 对 AgentCore 的 API 调用。

**PoC 1:用户在场,经网关做 OBO**

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 8}, "themeVariables": {"fontSize": "13px"}}}%%
flowchart LR
    U["用户"] -->|"登录,aud=agent-app"| C["Agent 客户端<br/>(只转发用户令牌)"]
    C -->|"tools/call + 用户令牌<br/>aud=agent-app"| G
    subgraph AC["Amazon Bedrock AgentCore"]
        G["Gateway<br/>• 入站 CUSTOM_JWT 校验<br/>• OpenAPI 目标,出站 OAuth"]
        O["🔑 OBO 令牌交换(AgentCore Identity)<br/>凭据提供者:grantType TOKEN_EXCHANGE<br/>输入用户令牌 → 输出下游令牌<br/>◆ GetResourceOauth2Token"]
        G ==>|"① 请求出站令牌"| O
    end
    O <==>|"② RFC 8693<br/>subject_token = 用户令牌"| K[("Keycloak<br/>签发新令牌<br/>外部身份提供商,不属于 AgentCore")]
    G ==>|"③ Bearer,aud=itsm-api"| D["模拟 ITSM API<br/>(Lambda)<br/>sub 不变:看到的是真实用户<br/>只返回该用户的数据"]
    linkStyle 2,3,4 stroke:#e65100,stroke-width:4px
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef obo fill:#ffe0b2,stroke:#e65100,stroke-width:4px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    classDef risk fill:#fff1f0,stroke:#b42318,stroke-dasharray:5 4,color:#0f1b2d
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    class G agentcore
    class O obo
    class K idp
    class U,C app
    class D down
```

**PoC 2:用户不在场,3LO + Token Vault**

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 8}, "themeVariables": {"fontSize": "13px"}}}%%
flowchart LR
    APP["应用(用户在场,仅一次)<br/>任务 → 用户 sub"] -->|"◆ ForUserId · ◆ GetResourceOauth2Token<br/>◆ CompleteResourceTokenAuth"| ID
    subgraph AC["Amazon Bedrock AgentCore"]
        ID["Identity<br/>• 3LO 凭据提供者(USER_FEDERATION)<br/>• 工作负载身份<br/>• Token Vault(用户 × agent)<br/><b>这里是 3LO,不是 OBO:没有交换,之后也不需要用户令牌</b>"]
    end
    B["用户浏览器<br/>登录 + 同意"] -->|"同意"| K[("Keycloak agent-bg<br/>外部身份提供商,不属于 AgentCore")]
    K -->|"授权码"| ID
    ID -.->|"刷新,直到会话上限"| K
    S["调度器(IAM 角色)<br/>唯一持有 ForUserId<br/>CloudTrail:不记用户 ID"] -->|"◆ ForUserId(任务中的 sub)<br/>◆ GetResourceOauth2Token"| ID
    ID -->|"访问令牌"| S
    S -->|"lambda:Invoke + Bearer"| D["模拟 ITSM API<br/>仅 IAM 可调用"]
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef obo fill:#ffe0b2,stroke:#e65100,stroke-width:4px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    classDef risk fill:#fff1f0,stroke:#b42318,stroke-dasharray:5 4,color:#0f1b2d
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    class ID agentcore
    class K idp
    class APP,B app
    class D down
    class S risk
```

**OBO 在哪里。** OBO 只在 PoC 1,图中橙色粗线那一段:网关出站 OAuth 向 AgentCore Identity 要令牌(①),Identity 到身份提供商用用户令牌做交换(RFC 8693,②),下游收到发给它自己、但仍带用户 `sub` 的令牌(③)。OBO 需要一张有效的用户令牌作为 `subject_token`,所以用户不在场的 PoC 2 里没有它;PoC 2 的令牌来自一次性的同意(3LO),并由 Token Vault 保管。

| | PoC 1 | PoC 2 |
|---|---|---|
| 流程 | OBO 令牌交换 | 3LO 授权码 + Token Vault |
| 凭据提供者 | `obo-test-keycloak`,带 OBO 配置 | `obo-test-bg-3lo`,没有 OBO 配置 |
| 调用时需要用户令牌 | 需要,每次调用 | 不需要 |
| 每次调用是否交换 | 是 | 否,Vault 取出已保存的令牌并刷新 |
| 下游看到的 `azp` | `agent-app` | `agent-bg` |

用到的 AgentCore 功能:

| 功能 | 服务 | PoC |
|---|---|---|
| MCP 网关、入站 `CUSTOM_JWT` 校验 | Gateway | 1 |
| OpenAPI 目标,出站 OAuth `TOKEN_EXCHANGE` | Gateway | 1 |
| 带 OBO 配置的 OAuth2 凭据提供者(RFC 8693,`actorTokenContent=NONE`) | Identity | 1 |
| OAuth2 凭据提供者,3LO(`USER_FEDERATION`) | Identity | 2 |
| 工作负载身份与允许的回调地址 | Identity | 2 |
| Token Vault:按用户 x agent 保存令牌,静默刷新 | Identity | 2 |
| `GetWorkloadAccessTokenForUserId` | Identity | 2 |
| `GetResourceOauth2Token` | Identity | 1(由网关服务角色调用)、2 |
| `CompleteResourceTokenAuth` | Identity | 2 |

v1 新增的功能(都有实测,见 `docs/results-v1.md`):

| 功能 | 服务 |
|---|---|
| 用代码包部署的 Runtime、IAM 入站;`runtimeUserId` 加 `InvokeAgentRuntimeForUser`;工作负载令牌通过 `WorkloadAccessToken` 请求头交给 agent | Runtime |
| 在 Runtime 内部调用 `GetResourceOauth2Token` 和 `CompleteResourceTokenAuth` | Identity |
| 入站按 `azp` 的自定义声明规则 | Gateway |
| 出站用独立交换客户端做 OBO | Gateway、Identity |
| 策略引擎加 Cedar 策略(先 LOG_ONLY,再 ENFORCE) | Policy |

不属于 AgentCore:Keycloak(签发令牌,决定同意页和会话上限)、IAM 角色、launcher、登记表、下游 API、CloudTrail。PoC 1 和 2 没有用 Runtime 和 Policy。所有版本都没有用 Memory、Runtime Instances、同意门户和 Harness。

**AWS 部署视图(us-west-2)**

[![AWS 部署架构](docs/architecture-aws.png)](docs/architecture-aws.svg)

全部资源在同一个账号、同一个区域。Keycloak 在一台 EC2 上充当企业身份提供商,前面是 CloudFront(安全组只放行 CloudFront 的回源流量,没有 SSH,运维走 Systems Manager)。模拟下游是一个没有公网入口的 Lambda。PoC 1 的网关目标目前指向一个已删除的端点,所以这套部署里只有 PoC 2 能完整跑通,图上已标出。点击图片可打开矢量版。图里是前两个 PoC。v1 在同一个账号和区域里新增了:自动任务 Runtime、launcher Lambda 和它的 EventBridge Scheduler 角色、DynamoDB 登记表、带策略引擎的后台网关、挂在同一个 Lambda 前面的令牌门控 HTTP API,以及记录 Runtime 数据事件的 CloudTrail trail。

**模式 → AgentCore 对照**

| 模式中的组件 | AgentCore 对应 | 本仓库是否实现 |
|---|---|---|
| 每用户认证锚点 | 身份提供商会话 + AgentCore 工作负载身份 | 部分:没有长期运行的每用户 broker |
| 把会话转发给工作区(用户在场) | Gateway 每次请求做 OBO 交换 | 是(PoC 1) |
| 服务端保管的刷新凭据 | Token Vault,3LO 授权码流程 | 是(PoC 2、v1) |
| 硬过期,到期由用户重新同意 | 身份提供商的刷新令牌 / 会话有效期 | 是(PoC 2、v1) |
| 限制谁能使用这份凭据 | PoC 2:只有调度器角色拥有 `ForUserId`。v1:没有主体持有 `ForUserId`,调用方需要对一个 Runtime 同时有 `InvokeAgentRuntime` 和 `InvokeAgentRuntimeForUser` | 是 |
| 无人值守的自动任务 | EventBridge Scheduler、launcher、自动任务 Runtime | 是(v1) |
| 工作区(每用户计算环境) | AgentCore Runtime 会话或 Runtime Instances | **否** |
| 同意页和授权列表 | 登记表前面的控制台 | **否**(用脚本代替) |

## 目录

```
poc/          kc_check.py   手工做 Keycloak 令牌交换,并直接检查下游
              e2e.py        PoC 1:以 alice 和 bob 的身份经网关做 MCP 调用
              bg3lo.py      PoC 2:setup / run / later(3LO 同意、调度器取令牌、过期)
              bg_test.py    OBO 网关上"没有用户令牌"的场景(E1–E4)
              cdp_login.mjs 用 Chrome(CDP)完成登录与同意页
downstream/   index.mjs     模拟 ITSM API(Lambda):校验 JWT 的 iss / exp / aud / 签名,只返回调用者自己的数据
scripts/      keycloak-setup.sh、cleanup.sh(PoC 1 和 2)
v1/           auto_agent/main.py   自动任务 Runtime 里的 agent 代码(connect、complete、use、gateway、probe)
              launcher/handler.py  launcher Lambda:查登记表,用 runtimeUserId 调用 Runtime
              deploy_*.sh          自动任务 Runtime、launcher 和登记表、后台网关、下游 HTTP API
              setup_roles.sh、setup_trail.sh、keycloak-v1.sh、attach_policy.py
              p0_*.py p1_*.py p2_*.py p3_*.py   对应 docs/results-v1.md 的各项检查
              lib.py rt.py gw_authorizer.py gw_direct.py   辅助脚本
docs/         setup.md、results.md(PoC 1 和 2,英文),design.zh-CN.md 和 results-v1.md(v1,中文),
              architecture-aws.svg / .png(前两个 PoC 的部署图)
LICENSE       MIT-0
```

## 运行

前提:开通了 AgentCore 的 AWS 账号、AWS CLI v2、Python 3、Node 22+、`uv` 和 `zip`(v1 会把 boto3 打进 Runtime 和 Lambda 的代码包)、一个可通过 HTTPS 访问的 Keycloak 26.2 及以上版本(不能是 `localhost`,AgentCore 要能访问 discovery 地址)、以及用 `--remote-debugging-port=9222` 启动的 Chrome(用于同意页这一步)。

**PoC 1 和 2**

```bash
cp .env.example .env && $EDITOR .env && set -a && . ./.env && set +a
# 按 docs/setup.md 创建 Keycloak 客户端、OAuth2 凭据提供者、
# 网关和目标、工作负载身份以及 IAM 角色
python3 poc/e2e.py                 # PoC 1
python3 poc/bg3lo.py setup         # PoC 2:alice 在浏览器里同意
python3 poc/bg3lo.py run           # 调度器取她的令牌,并跑反向测试
python3 poc/bg3lo.py later         # 分别在访问令牌过期后、会话上限之后再运行
```

**v1** 需要上面搭好的 realm、3LO 凭据提供者 `obo-test-bg-3lo` 和 Lambda `obo-test-itsm`。每个脚本开头的注释写明了它需要什么,先读再跑。

```bash
# 在 Keycloak 主机上:交换客户端、受众映射器、120 秒的访问令牌
BGEX_SECRET=... bash v1/keycloak-v1.sh
bash v1/deploy_downstream_api.sh        # 打印 API 地址:导出 DOWNSTREAM_URL
bash v1/deploy_auto_runtime.sh          # 打印 agentRuntimeId:导出 AUTO_RUNTIME_ID
aws bedrock-agentcore-control update-workload-identity --name "$AUTO_RUNTIME_ID" \
  --allowed-resource-oauth2-return-urls "$KC_ISSUER/.well-known/openid-configuration"
bash v1/deploy_launcher.sh
export BGEX_SECRET=... && bash v1/deploy_bg_gateway.sh    # 打印网关 ID 和地址:导出 BG_GATEWAY_ID
python3 v1/attach_policy.py LOG_ONLY    # 之后改成 ENFORCE
GATEWAY_URL=<网关地址> bash v1/deploy_auto_runtime.sh      # 重跑一次,让 Runtime 知道网关地址
python3 v1/p0_v2_runtime_grant.py       # 经 Runtime 授予(有浏览器步骤),再使用
python3 v1/p1_launcher.py               # Scheduler -> launcher -> Runtime -> Vault -> 下游
python3 v1/p2_gateway.py                # 经后台网关
```

其余检查(`p0_v1_identity_paths.py`、先跑 `setup_roles.sh` 的 `p0_v2_iam.py`、`p2_vault_freshness.py`、需要 `KC_ADMIN_CMD` 的 `p3_revoke.py`、用 `setup_trail.sh` 开的 CloudTrail)见 `docs/results-v1.md`。

## 发现

**PoC 1 和 2**(详见 `docs/results.md`)

- 用户在场时 OBO 成立:下游看到的 `sub` 不变,`aud` 被改写,每个用户只拿到自己的数据。agent 客户端从不持有下游凭据。
- Keycloak 的标准令牌交换不写 `act` 声明,下游只能通过 `azp` 判断这是 agent 的调用。要写入 `act` 需要 Keycloak 的预览功能"委托",它要求每个会话都由用户同意,不适合无人值守的场景。
- 没有用户令牌时,网关会接受 agent 自己的 client-credentials 令牌,需要在入站做限制(v1 测了按 `azp` 的规则,`allowedClients` 没有测)。
- Token Vault 能做到"同意一次,稍后运行":在身份提供商的会话上限之内会静默刷新;超过上限后,Vault 不再返回令牌,而是返回新的授权链接。
- `GetWorkloadAccessTokenForUserId` 接受的是未经验证的用户 ID 字符串。谁持有这个 IAM 权限,谁就能取出任何已经同意过的用户的令牌。PoC 2 里调度器持有它,v1 不再需要。
- CloudTrail 会记录 `ForUserId` 的调用,但不记录传入的用户 ID。

**v1**(详见 `docs/results-v1.md`)

- 用户 JWT 路径和用户标识路径写入的是不同的 Token Vault 条目(试了五种用户标识写法,都对不上)。
- 调用方不能为 Runtime 的托管工作负载身份取工作负载令牌,Vault 条目又按工作负载身份分开,所以授予也在 Runtime 里完成。会话绑定会拒绝另一个用户的标识。
- 带 `runtimeUserId` 调用需要同时有 `InvokeAgentRuntime` 和 `InvokeAgentRuntimeForUser`。没有主体需要 `GetWorkloadAccessTokenForUserId`。
- `InvokeAgentRuntime` 的 CloudTrail 数据事件有 `runtimeSessionId`,没有 `runtimeUserId`。launcher 把用户的哈希放进会话 ID,并写一条运行记录。
- Scheduler、launcher、Runtime、Vault、下游整条链路跑通。授权过期、从未授权、已关闭时都不会调用下游。
- 后台网关:`azp` 规则会拒绝另一个客户端的令牌;独立的交换客户端让下游看到不同的 `azp`;Cedar 在 ENFORCE 下只列出并放行读工具。
- 网关入站会拒绝剩余有效期不足约一分钟的令牌,所以 60 秒的访问令牌在网关后面用不了,120 秒可以。Vault 在剩余约一分钟时刷新,身份提供商会话的最后一分钟不可用。
- 撤销(结束会话,或删除同意)有效。Vault 一直返回缓存的令牌,直到剩余约一分钟,所以窗口最多约一分钟;已经签发的令牌用到 `exp`。
- 按文档(没有实测):Harness、Gateway 的 3LO 目标和同意门户都要求入站带 JWT,所以用户不在场时没有托管组件能取用户的令牌。
- 没有测:Entra ID 的 OBO、真实的以天计的会话上限和空闲超时、按用户组区分时长、条款确认、Runtime Instances、网关到私有下游的连通、显式 Deny 和 SCP、真正的授权页面。

## 安全说明

- 密钥文件(`secrets.env`、`.env`)已加入 `.gitignore`。测试用户为了方便使用了密码模式,生产环境不要这样做。
- 最初几个 PoC 的模拟下游没有公网入口,并自己校验令牌。Keycloak 以 `start-dev` 模式运行,前面有一层屏蔽管理路径的 CDN。这两者都只是测试脚手架。
- v1 代码(`v1/`)是测试脚手架,没有加固。自动任务 Runtime 里的 agent 保留了测试用的模式(`probe`,以及从调用方取工具名和参数的 `gateway` 模式);Runtime 用 PUBLIC 网络模式;它的执行角色对工作负载身份用了名称前缀通配,还能读凭据提供者的客户端密钥;v1 的下游是令牌门控的公网 HTTP API。真正使用前要去掉或收紧这些。
- v1 还没有清理脚本。它的资源名以 `obo-test-v1` 或 `obo_test_v1` 开头,用完请手动删除。
- 每个脚本都会在你自己的 AWS 账号和身份提供商里创建或修改资源。运行前请先阅读,不要对不属于你的账号运行。
- 用完后请阅读并运行 `scripts/cleanup.sh`。测试资源会持续运行并产生费用。
