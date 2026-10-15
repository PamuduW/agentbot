# Atlassian Cloud read MCP

`atlassian_read` is a local stdio server for Jira and Confluence. It exposes ten
fixed read tools and uses a separate scoped API token for each product. All
requests are `GET` requests to `https://api.atlassian.com`; the server does not
follow redirects, returned URLs, attachments, or arbitrary request arguments.
Issue reads explicitly disable view-history updates.

The catalog entry is available for setup. Live Cloud endpoint checks and native
client acceptance have not yet run. Offline tests cover the MCP protocol,
credential routing, argument rejection, response limits, and native rendering.

## Create the credentials

Open [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
and choose **Create API token with scopes**. Create one token for Jira and one
for Confluence. Select an expiry and only the read scopes below. Avoid the
unscoped token option. See Atlassian's
[token creation instructions](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).

Scopes limit actions, while your account permissions determine the projects,
issues, spaces and pages you can access. These scopes do not restrict the token
to one project or space. Use an account with the intended content access.

### Jira scopes

The following are the documented granular scopes for the fixed platform reads:

```text
read:application-role:jira
read:audit-log:jira
read:avatar:jira
read:comment:jira
read:comment.property:jira
read:field-configuration:jira
read:group:jira
read:issue:jira
read:issue-details:jira
read:issue-meta:jira
read:issue-security-level:jira
read:issue.changelog:jira
read:issue.vote:jira
read:issue-type:jira
read:issue-type-hierarchy:jira
read:project:jira
read:project-category:jira
read:project-role:jira
read:project-version:jira
read:project.component:jira
read:project.property:jira
read:status:jira
read:user:jira
```

If the scope picker offers `read:jira-work`, the endpoint documentation lists it
as a classic alternative for issue, search, comment and project reads. Personal
token picker availability has not been verified. Sources:
[issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get),
[GET search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-get),
[comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-get),
[projects](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-search-get),
and the [published OpenAPI specification](https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json).

Boards additionally require `read:board-scope:jira-software` and
`read:project:jira`. Sprint lists require `read:sprint:jira-software` and a Scrum
board. These scopes are documented for the APIs and service-account tokens;
their availability in your personal token picker still needs checking. Without
them, platform reads remain usable and the board/sprint tools may return 403.
See [board endpoints](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/)
and [service-account scope selection](https://support.atlassian.com/jira-align/kb/configuring-jira-connector-credentials-using-jira-cloud-api-service-account/).

### Confluence scopes

| Scope | Tools |
|---|---|
| `read:content-details:confluence` | CQL search |
| `read:page:confluence` | Page metadata and storage-format body |
| `read:space:confluence` | Space list |
| `read:comment:confluence` | Root footer and inline comments |

See the endpoint documentation for
[search](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/),
[pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/),
[spaces](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-space/),
and [comments](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-comment/).

The local server cannot introspect all scopes on a personal API token. Successful
read calls do not prove that a token lacks write scopes. Confirm the selected
scopes when creating each token. The server's fixed read surface is enforced
independently of the token's permissions.

## Supply configuration and register clients

Find your site's UUID `cloudId` at
`https://YOUR-SITE.atlassian.net/_edge/tenant_info`. Use the UUID, not the site
name. See [Atlassian's Cloud ID instructions](https://support.atlassian.com/jira/kb/retrieve-my-atlassian-sites-cloud-id/).
The runtime always uses the scoped-token gateway paths
`/ex/jira/{cloudId}` and `/ex/confluence/{cloudId}/wiki/...`.

Open `agentbot`, select **5. Token Config**, then **Atlassian tokens**. Choose
**Save or replace** and enter your account email, Cloud ID UUID, Jira token and
Confluence token. Both token prompts hide input. Confirm that you selected only
read scopes when creating the tokens.

Agentbot saves these settings in
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot/atlassian.json`. The directory must
have mode `0700`; the file has mode `0600` and is replaced atomically. This is
a private plaintext credential file, following the GitLab token storage pattern.
The screen shows token fingerprints, provides **Check Cloud reads**, and lets
you remove the saved settings. Checks only read one page of Jira projects and
Confluence spaces; they do not introspect the tokens' scope lists.

The local MCP loads the saved settings automatically. Register the clients once:

```bash
agentbot mcp plan --select atlassian_read --targets claude codex cursor
agentbot mcp setup --select atlassian_read --targets claude codex cursor --yes
```

If you registered the earlier environment-only configuration, rerun setup to refresh its credential handling. Restart your agent clients after registration. No token exports are required
when using saved settings. The CLI also exposes `agentbot atlassian-token status`,
`check`, and `remove`. Its `set` action accepts the four named settings as JSON
on stdin; tokens must never be passed as command arguments.

### Certificate trust on corporate networks

The Atlassian client uses Python's system CA trust store for both the token
check and MCP tools. On WSL, a company CA already installed in the Linux trust
store is available automatically, including in new terminals and agent-launched
MCP servers. No shell export is needed. Certificate and hostname verification
remain enabled.

Optional `SSL_CERT_FILE` and `SSL_CERT_DIR` settings select a custom trust store.
Codex registration forwards these variables when present; rerun MCP setup after
updating an existing registration if you use these overrides. Restart agent
clients after updating Agentbot. See the
[Python SSL documentation](https://docs.python.org/3/library/ssl.html#ssl.create_default_context).

### Optional environment overrides

Each nonempty runtime variable overrides its saved value. A complete set can
also run without a saved file. Run these Bash commands in a private terminal
with shell tracing disabled; the token prompts hide input:

```bash
set +x
export ATLASSIAN_MCP_EMAIL='you@example.com'
export ATLASSIAN_MCP_CLOUD_ID='your-site-cloud-uuid'
read -rsp 'Jira scoped read token: ' JIRA_MCP_READ_TOKEN; printf '\n'
read -rsp 'Confluence scoped read token: ' CONFLUENCE_MCP_READ_TOKEN; printf '\n'
export JIRA_MCP_READ_TOKEN CONFLUENCE_MCP_READ_TOKEN
```

Launch your agent client from that same terminal to use the overrides. Keep a
copy of the tokens in a password manager. Do not place token values in repository
files, command arguments, or client configuration. Claude, Cursor and
Antigravity inherit the environment without an `env` block; Codex forwards the
four optional variables and `XDG_CONFIG_HOME` by name.

`agentbot mcp status` checks owned configuration and resolves the saved settings
plus environment overrides. Unsafe or incomplete settings are reported as an
authentication configuration error. Its
`--live` flag does not perform Cloud API or token-scope validation. Missing or
invalid configuration causes the server to exit with a sanitized error on
stderr.

## Tool behavior

| Tool | Inputs and pagination |
|---|---|
| `jira_search` | `jql`, optional `limit` and `next_page_token` |
| `jira_issue` | `issue_id`: key such as `DEMO-1`, or numeric ID as a string |
| `jira_comments` | `issue_id`, optional `limit` and `start_at` |
| `jira_projects` | Optional `limit` and `start_at` |
| `jira_boards` | Optional `limit` and `start_at` |
| `jira_sprints` | Integer `board_id`, optional `limit` and `start_at` |
| `confluence_search` | `cql`, optional `limit` and `cursor` |
| `confluence_page` | Numeric `page_id` as a string |
| `confluence_spaces` | Optional `limit` and `cursor` |
| `confluence_comments` | `page_id`, optional `kind` (`footer` or `inline`), `limit` and `cursor` |

Each list/search call requests one page, defaulting to 20 records and allowing
up to 100. Use Jira's `nextPageToken` or offset metadata to continue. For
Confluence, extract the `cursor` value from the returned `_links.next` URL and
pass it to the same tool. The server does not fetch that URL. Confluence comment
lists contain root comments; fetching nested replies is not implemented.

Requests have a 30-second HTTP timeout and responses have a 4 MiB size limit.
Timeouts, rate limits and HTTP failures return sanitized errors without retrying.
All returned content is marked untrusted. Configured token values, their Basic
authentication encodings, and known credential fields are redacted.

## Validation

From the Agentbot repository:

```bash
.venv/bin/python -m unittest tests.test_atlassian_read -v
env -u NO_COLOR bash tests/run.sh
```

For live acceptance, first supply the four runtime variables above. The opt-in
test uses environment values rather than saved settings. Then choose
an issue, page and Scrum board you are permitted to read:

```bash
export ATLASSIAN_MCP_TEST_ISSUE='DEMO-1'
export ATLASSIAN_MCP_TEST_PAGE_ID='12345'
export ATLASSIAN_MCP_TEST_BOARD_ID='7'
AGENTBOT_TEST_ATLASSIAN_MCP=1 \
  .venv/bin/python -m unittest tests.integration.test_atlassian_read_live -v
```

The opt-in test calls all ten tools plus inline-comment reads. It does not print
tokens or returned issue/page/comment content. It does not attempt mutations to
test token scope restrictions. Native Claude, Codex and Cursor launches against
your real site remain a separate acceptance check.
