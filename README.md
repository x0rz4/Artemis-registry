# Artemis Registry

Artemis 官方工具与 Skill 更新源，独立于主程序发布。客户端读取公开稳定索引，自动检查、手动选择更新，不需要 GitHub Token。

工具版本、下载摘要、架构、上游地址和许可证锁定在 `packaging/tools-lock.json`。首批 linux/amd64 八个工具；linux/arm64 七个（kscan 无该架构）。Skill 源码和版本分别位于 `skills/`、`packaging/skills-lock.json`。

## 发布

1. 修改锁文件中的明确版本、上游 URL、SHA-256、许可证与更新内容，禁止使用上游 latest。
2. `python -m unittest discover -s scripts -p 'test_*.py' -v`。
3. 提交源码，创建新的语义版本标签，例如 `v1.0.1`，推送标签。
4. Actions 打包 ZIP，检查两种架构，并只运行帮助或版本命令。全部通过后上传草稿 Release，重新下载比对所有资产，再公开 Release，最后提交 `channels/stable.json`。

已存在的 Release 拒绝覆盖，失败不会推进稳定索引。未完成草稿也需要明确处理后用新版本发布。回退稳定渠道可将旧 Release 附带的 `stable.json` 作为 `channels/stable.json` 提交；客户端仍由用户确认更新，不会删除本地条目。

本地打包：`python scripts/build_registry.py --release v1.0.1 --output dist`。输出目录须为空。构建缓存按上游摘要命名，每次使用仍校验摘要；不执行下载的二进制。CI 运行检查使用 `--smoke linux/amd64` 或 `--smoke linux/arm64`。

## 客户端协议

`channels/stable.json` 包含固定 `release` 和工具、Skill 清单的 URL/SHA-256。两个清单的 `items` 包含名称、版本、描述、变更、许可证和 `assets`。每个资产包含平台、ZIP 地址、包摘要、解包内容摘要、入口文件。所有 URL 指向同一个确定的 Release。

内容摘要：对每个普通文件计算 SHA-256，以相对路径按字典序排序，拼接 `sha256 + 两个空格 + POSIX 路径 + 换行`，再计算整体 SHA-256。目录、时间戳和权限不参与；不允许链接。工具入口需另行验证 ELF 架构并设置执行权限。

## 来源与许可

发布脚本及继承的 Artemis Skill 遵循根目录 AGPL-3.0 许可，来源为 [Artemis](https://github.com/x0rz4/Artemis) / [ARTEX](https://github.com/Autumn-27/ARTEX)。Playwright Skill 来源于 Microsoft Playwright CLI，单独保留 Apache-2.0 许可。每个工具 ZIP 包含上游许可证、来源及固定下载摘要；MIT 工具和 GPL-3.0 kscan 按各自许可使用。kscan 对应版本的完整上游源码归档与二进制一同发布，地址和摘要写入清单。

工具仅用于获授权环境。CI 不发起目标扫描。
