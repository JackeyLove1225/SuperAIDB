# 用户可见提示信息常量
# 所有中文提示集中管理，方便多语言扩展和统一修改

# ── 表操作 ──
MSG_TABLE_NOT_FOUND = "表 '{name}' 不存在于配置中"
MSG_TABLE_EXISTS    = "数据库中已有同名表「{name}」，需要覆盖吗？这可能导致原表数据丢失！"
MSG_TABLE_CREATED   = "已创建表 {name}"
MSG_TABLE_DELETED   = "已删除 {count} 个表"
MSG_TABLE_RENAMED   = "已重命名 {old} → {new}"

# ── 字段操作 ──
MSG_FIELD_NOT_FOUND    = "字段 '{name}' 不存在"
MSG_FIELD_TYPE_CHANGED = "已修改 {table}.{field}: {old} -> {new}"
MSG_FIELD_ADDED        = "已加入新字段：{name} ({type})"
MSG_FIELD_DELETED      = "已从 {table} 删除字段 {field}"
MSG_FIELD_EXISTS        = "此字段已存在"
MSG_FIELD_NN_SET        = "已将 {table}.{field} 设为非空"
MSG_FIELD_NN_FAILED     = "该字段存在空值，无法设为非空。请先补全有效数据后再操作"

# ── 系统列保护 ──
MSG_SYS_COL_PROTECTED  = "{column} 是系统主键，不允许{action}"
MSG_SYS_ID_INSERT      = "id 是系统主键，由系统自动生成，不允许手动指定。请去掉 id 字段后重新插入"

# ── 外键 ──
MSG_FK_SET         = "已设置外键: {table}.{col} -> {ref_table}.{ref_col}"
MSG_FK_TYPE_MISMATCH = "外键字段 {col} 类型为 {fk_type}，目标主键 {ref}.{ref_col} 类型为 {ref_type}，两者不一致，无法设置外键。请先统一类型"
MSG_FK_DELETE_BLOCK = "字段 {field} 被 {refs} 的外键引用，请先解除外键约束再删除"
MSG_FK_IS_FK_FIELD  = "字段 {field} 是外键字段，当前关联表【{ref}】。确认删除请再次执行并设置 force=true，系统将自动解除外键并删除字段"

# ── 索引 ──
MSG_INDEX_CREATED       = "已创建索引 {name}"
MSG_INDEX_DELETED       = "已删除索引 {name}"
MSG_INDEX_EXISTS        = "索引 {name} 已存在"
MSG_INDEX_DUPLICATE     = "该列存在重复值，无法创建唯一索引。请先清理重复数据后再操作"

# ── 查询 ──
MSG_QUERY_EMPTY         = "查询内容为空"

# ── 路由 ──
MSG_ROUTE_FAILED         = "无法确定操作: {instruction}"
MSG_AMBIGUOUS_FIELD      = "字段 '{col}' 在 {tables} 中都存在，请指定表名"
MSG_TABLE_DB_NOT_FOUND   = "表 '{name}' 不存在，请确认表名是否正确"
MSG_FIELD_DB_NOT_FOUND   = "字段 '{name}' 在表 {table} 中不存在"
MSG_DATABASE_NOT_FOUND   = "表 '{tbl}' 不存在"

# ── 配置一致性 ──
MSG_CONSISTENCY_BLOCK   = "操作被阻止: {reason}"
MSG_CONSISTENCY_FIELD_DIFF = "表 {table} 的字段 '{field}' 属性不一致：{diff}"
MSG_CONSISTENCY_INDEX_MISSING = "表 {table} 的索引 '{name}' 不存在于数据库中"

# ── 插入/数据操作 ──
MSG_INSERT_OK           = "已插入{table}数据：{cols}"
MSG_INSERT_FAIL         = "插入失败: {reason}"
MSG_DELETE_OK           = "已从 {table} 删除 {count} 条记录"
MSG_DELETE_BLOCK_FK     = "删除被阻止：数据被其他表的外键引用，请先解除外键约束再删除"
MSG_UNIQUE_CONSTRAINT   = "违反唯一性约束：字段 '{field}' 的值已存在，不能重复。请使用不同的值"
MSG_NOTNULL_CONSTRAINT  = "违反非空约束：{field} 不能为空"
MSG_FORCE_CONFIRM       = "确认请使用 force=True"
MSG_OPERATION_CANCELLED = "操作已取消，未做任何更改"

# ── 工具级 ──
MSG_SPECIFY_TABLE       = "请指定表名"
MSG_SPECIFY_TABLE_AND_FIELD = "请指定表名和字段名"
MSG_SPECIFY_TYPE        = "请指定要修改的数据类型（如 INTEGER/TEXT/FLOAT）"
MSG_UNSUPPORTED_TYPE    = "不支持的类型: {type}"
MSG_DATA_FORMAT_JSON    = "data 格式错误，请使用 JSON 格式"
