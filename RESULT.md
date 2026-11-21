## 承認待ちの Deploy を自動承認する試み (2026-11-21T11:07:02Z)

### 実行前
- #8 waiting/ 1810c2b
- #7 completed/cancelled ccdf5e9
- #6 completed/cancelled 3ba18cd
- #5 completed/cancelled 697d29d
- #4 completed/failure a17ef70

### run 44869186842 の pending_deployments
```
[{"environment":{"id":27492517034,"node_id":"EN_kwDOVUwPys8AAAAGZq6Eqg","name":"live","url":"https://api.github.com/repos/sollall/trade-bot-factory/environments/live","html_url":"https://github.com/sollall/trade-bot-factory/deployments/activity_log?environments_filter=live"},"wait_timer":0,"wait_timer_started_at":null,"current_user_can_approve":false,"reviewers":[{"type":"User","reviewer":{"login":"sollall","id":62292218,"node_id":"MDQ6VXNlcjYyMjkyMjE4","avatar_url":"https://avatars.githubusercontent.com/u/62292218?v=4","gravatar_id":"","url":"https://api.github.com/users/sollall","html_url":"https://github.com/sollall","followers_url":"https://api.github.com/users/sollall/followers","following_url":"https://api.github.com/users/sollall/following{/other_user}","gists_url":"https://api.github.com/users/sollall/gists{/gist_id}","starred_url":"https://api.github.com/users/sollall/starred{/owner}{/repo}","subscriptions_url":"https://api.github.com/users/sollall/subscriptions","organizations_url":"https://api.github.com/users/sollall/orgs","repos_url":"https://api.github.com/users/sollall/repos","events_url":"https://api.github.com/users/sollall/events{/privacy}","received_events_url":"https://api.github.com/users/sollall/received_events","type":"User","user_view_type":"public","site_admin":false}}]}]
```
- environment id = 27492517034
### 承認 API の結果
```
{"message":"Validation Failed","errors":"No pending deployment requests to approve or reject","documentation_url":"https://docs.github.com/rest/actions/workflow-runs#review-pending-deployments-for-a-workflow-run","status":"422"}gh: No pending deployment requests to approve or reject (Validation Failed)
```
- 承認できませんでした (rc=1)

### 承認後 (最長10分)
- 11:07Z run 44869186842 = waiting/
- 11:07Z run 44869186842 = waiting/
- 11:08Z run 44869186842 = waiting/
- 11:08Z run 44869186842 = waiting/
- 11:09Z run 44869186842 = waiting/
- 11:09Z run 44869186842 = waiting/
- 11:10Z run 44869186842 = waiting/
- 11:10Z run 44869186842 = waiting/
- 11:11Z run 44869186842 = waiting/
- 11:11Z run 44869186842 = waiting/
- 11:12Z run 44869186842 = waiting/
- 11:12Z run 44869186842 = waiting/
- 11:13Z run 44869186842 = waiting/
- 11:13Z run 44869186842 = waiting/
- 11:14Z run 44869186842 = waiting/
- 11:14Z run 44869186842 = waiting/
- 11:15Z run 44869186842 = waiting/
- 11:15Z run 44869186842 = waiting/
- 11:16Z run 44869186842 = waiting/
- 11:16Z run 44869186842 = waiting/

### オーナー作成の PR
- PR はまだありません (create は GITHUB_TOKEN では禁止)
