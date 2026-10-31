# Auth0 配置清单

目标站点：`https://openinvest.involutionhell.com`。配置期间保持服务器的 `HOSTED_REGISTRATION_OPEN=0`。

## 套餐

先选 Free。2026-10-31 的[官方定价](https://auth0.com/pricing)为 $0/月、最高 25,000 月活用户，包含社交登录。首发约 10 名用户的基础登录需求在该额度内。使用租户默认域名即可；无需为本次接入购买套餐。MCP 开关与授权流程仍须在实际租户上验证，遇到功能限制先核对原因。

## 1. 创建租户和网页应用

在 [Auth0 Dashboard](https://manage.auth0.com/) 创建独立租户。记录实际 Domain，不需要购买自定义登录域名。

在 **Applications → Applications** 创建 `openInvest Portal`，类型选 **Regular Web Applications**。

| 字段 | 值 |
| --- | --- |
| Allowed Callback URLs | `https://openinvest.involutionhell.com/auth/callback` |
| Allowed Logout URLs | `https://openinvest.involutionhell.com/connect/` |
| Allowed Web Origins | `https://openinvest.involutionhell.com` |
| Grant Types | Authorization Code；网页应用无需开启 Implicit 或 Password |

将 Domain、Client ID、Client Secret 对应填入 Oracle 的 `/etc/openinvest-hosted.env`：

```dotenv
HOSTED_AUTH0_ISSUER=https://实际租户域名/
HOSTED_AUTH0_CLIENT_ID=网页应用的ClientID
HOSTED_AUTH0_CLIENT_SECRET=网页应用的ClientSecret
```

使用 `sudoedit /etc/openinvest-hosted.env` 编辑。Client Secret 不放进网页环境变量、Git 或聊天。

## 2. 配置 Google 和 GitHub

在 **Authentication → Social** 创建或编辑 `google-oauth2` 和 `github`，并在应用列表中启用 `openInvest Portal`。不要安装按邮箱自动合并账户的 Action。

Google Cloud 创建 Web OAuth 客户端；GitHub Developer Settings 创建 OAuth App。两个提供商的回调均填：

```text
https://实际租户域名/login/callback
```

这是 **Google/GitHub → Auth0** 的回调，与第一步 **Auth0 → openInvest** 的 `/auth/callback` 不同。将提供商各自的 ID / Secret 填入 Auth0 Social Connection。Google 若仍处于测试发布状态，需先将测试登录用户加入其允许列表。

为动态注册的 MCP 客户端启用这两个连接；如控制台提供 **Promote Connection to Domain Level**，按租户规则启用。不要仅验证网页应用可登录便认定 MCP 客户端也能登录。

## 3. 创建 MCP API

在 **Applications → APIs** 创建 `openInvest MCP`：

| 字段 | 值 |
| --- | --- |
| Identifier / Audience | `https://openinvest.involutionhell.com/mcp` |
| Signing Algorithm | RS256 |
| Permissions | `invest:read`、`invest:write`、`invest:analyze` |

首发不需要额外角色体系。若启用 RBAC，必须为测试用户分配上述权限，确认访问令牌的 `scope` 包含获准权限。允许第三方 MCP 应用请求此 API 的用户授权；保留用户授权流程，不配置全局跳过同意。

在 **Settings → Advanced** 开启：

- Dynamic Client Registration。
- Resource Parameter Compatibility Profile。

发现文档应公布注册端点及 PKCE S256 支持。若租户不提供 resource 兼容开关，专用租户可设置 Default Audience 为上面的 MCP API Identifier；无论采用哪种方式，都需实际验证令牌 audience。

## 4. 安装登录 Action

创建 **Post Login** Action，复制同目录 [auth0-action.js](auth0-action.js)，添加四项 Action Secrets：

| Action Secret | 来源 |
| --- | --- |
| `PORTAL_CLIENT_ID` | 第一步网页应用的 Client ID |
| `MCP_AUDIENCE` | `https://openinvest.involutionhell.com/mcp` |
| `PORTAL_ORIGIN` | `https://openinvest.involutionhell.com` |
| `HOSTED_ACTION_SECRET` | 服务器 `/etc/openinvest-hosted.env` 中同名值 |

部署 Action 后，将其加入 Login 流程并应用。此 Action 将 MCP 首次授权带到网页初始化，再返回 Auth0 继续授权；普通网页登录不执行该重定向。

服务器的 Action Secret、会话密钥、管理员凭证和数据加密密钥独立生成，不使用网页应用的 Client Secret 替代。服务器配置不自动上传到 Auth0。

## 5. 配置完成后的交接

可以在聊天里提供 **租户 Domain 和网页应用 Client ID**。Client Secret 仅填服务器配置文件；Action Secret 从服务器复制到 Auth0。

部署侧随后执行配置检查、启动网关、配置 HTTPS 回源及 Pages `HOSTED_GATEWAY_ORIGIN`，再用合成账户完成：

1. Google 和 GitHub 首次／重复登录，确认不同身份对应独立账本。
2. ChatGPT 和至少一个终端客户端 OAuth、查询及幂等写入。
3. 凭证撤销、研究开关、COS 归档／恢复，以及 24 小时稳定性验收。

公开注册仍保持关闭，直到验收完成。配置检查只验证本地字段格式，不替代真实登录和令牌验证。

## 官方参考

- [Auth0 MCP：DCR、resource 兼容与连接配置](https://auth0.com/blog/secure-csharp-mcp-server-with-auth0/)
- [Google Social Connection](https://developer.auth0.com/resources/labs/authentication/google-social-connection-to-login)
- [GitHub Social Connection](https://developer.auth0.com/resources/labs/authentication/authenticate-using-github)
- [MCP audience 排查](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0)
