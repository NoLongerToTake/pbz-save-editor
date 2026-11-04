# 3项组件来源

- Python、Tkinter/Tcl：由用户的Python安装提供，遵循对应项目许可证。
- PyCryptodome：通过requirements.txt安装，保留上游组件自身许可。
- oozextract 0.5.7：源码来自 https://github.com/lvlvllvlvllvlvl/oozextract ，其Cargo清单声明MIT。保留源码、Cargo清单及上游README，位于third_party/oozextract。decoder是使用该库的命令行入口，Cargo.lock锁定依赖。

物品名称、描述、资源路径和任务结构来自游戏数据，仅用于存档识别。游戏相关内容的权利归相应权利人。

免安装包中的Windows解码器与先前构建目录中的产物SHA-256一致；本次公开整理未更换该程序。Python运行时和第三方Python二进制依赖不纳入源码仓库。
