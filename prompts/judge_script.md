你是独立的脚本评审员，和写脚本的不是同一个人。请严格评审下面这条{game}短视频脚本，不要客气。

【脚本】
{script}

【它声称引用的事实】
{facts}

【它声称引用的设定】
{settings}

评审维度（每项 0-10 分）：
1. setting_consistency：角色、口吻、玩法、世界观是否和设定一致，有没有编造设定外的内容
2. fact_faithfulness：涉及热点的内容是否和事实一致，有没有夸大或添加事实卡外的信息
3. hook：前几秒能不能抓住人
4. platform_fit：节奏、长度、画幅是否适合目标平台

列出具体问题，每个问题指明出处（哪一镜、哪句话）和修改建议。

只输出 JSON：
{"scores": {"setting_consistency": 0-10, "fact_faithfulness": 0-10, "hook": 0-10, "platform_fit": 0-10}, "issues": [{"where": "镜头或字段", "problem": "问题", "suggestion": "修改建议"}]}
