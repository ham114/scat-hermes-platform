# 离线创作材料

11 类模板与一个可离线校验的调查候选，全部位于发布目录 skills/ 之外。模板需填写、脱敏和评测；通过静态校验也不自动发布或执行。未接入的模型/对比/评估/物化/实验/报告只提供设计模板，不能发布不可用依赖。

用已有 Conda 执行 python -B scripts/skills_tool.py check-draft --path authoring/drafts/scat-investigation-candidate。候选任何文件变化都会改变报告摘要，需重新校验和评测。没有作者业务 API、发布指针切换或技能脚本执行器，S7 再实现。
