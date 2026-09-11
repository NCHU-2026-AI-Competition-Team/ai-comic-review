"""pytest 全局配置：测试使用独立的临时存储目录。"""

import os
import tempfile

# 在导入应用之前指定存储目录，避免测试污染仓库下的 storage/
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="ai-comic-review-test-")
