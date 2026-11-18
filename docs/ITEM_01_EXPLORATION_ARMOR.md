# ITEM-01 探索装备批次

## 当前实现

`world.equip_armor`、`world.unequip_armor`、`world.equip_shield`、
`world.unequip_shield` 按物品实例 ID 操作直接携带、可访问的装备。
旧装备仍归角色携带，只清除装备栏绑定。戴盾自动收起另一只手物品；
双手必需武器会收起，可单手可双手武器仍在主手并改为单手握。
卸盾且另一只手空闲时，可单双手武器恢复双手握。
探索穿脱不推进世界时间，也不修改战斗行动预算。

`EquipWeapon` 可省略 `grip`；旧 `one_handed`／`two_handed` 值仍接受，
实际握法由武器属性和另一只手是否占用决定。拿起双手必需武器时，
冲突的盾牌和另一只手物品自动收起。物品的归属、容纳和数量不变。

Schema52 将 /19 角色的装备实例 ID、握法和指定的潜行劣势存到
`character_equipment`。基础 AC 由 `character_attributes.ac` 持有。
CharacterFacts 在原命令 UnitOfWork 内共同写入，两行角色事实随事件和回执一起提交，
失败一同回滚。物品本身的状态仍由 WorldFacts 管理。

当前读取沿 `CharacterRuntime` 的装备字段直接复用 `SessionMemory` 的 `ActiveItem`。
有效字段给换装预览和规则快照提供已加载的绑定和物品资料。未提交候选和较早的固定读取
使用隔离值。交给 RE 的快照持有装备 ID、规则标记和 AC 等独立的值。
`StateMachine.read_character_ac` 返回 `CharacterAC(base, effects, effective)`；
其中 `effects` 只累计当前启用且可解析为标量的目标 AC 效果，
不含仅适用于某次攻击的掩护。RE 攻击仍接收基础 AC 并自行处理效果。

## 受支持范围与门禁

目前仅对可确认默认护甲算法、职业训练与原有 AC 相符的 Fighter 1／
Cleric 1 角色重算 AC。允许符合训练条件的普通轻甲、中甲、无力量速度门槛的
重甲与盾牌，覆盖皮甲、链甲衫、环甲（Ring Mail）与普通盾牌。
使用 RE `armor_class`、`defense_ac_bonus` 和训练函数；
来源不明的既有 AC 拒绝换装，原值保留。魔法、需同调、有被动效果、
有使用次数或力量速度门槛的护甲暂不在公开换装范围。
装备单件数量必须是 1；旧开发存档的兼容不是本批目标。

完整战斗前置仍由 RE 审核。初始 /19 职业装备若不满足原通用门禁，
仅 Fighter 1／Cleric 1 且 RE 已审核的装备闭包可进入；
其他初始限制继续生效。装备组合能否用于普通攻击由 RE 的战斗准入决定。

## 验收证据与待审

定向测试位于 `tests/test_exploration_armor_equip.py`、
`tests/test_exploration_armor_attack.py` 和
`tests/test_exploration_weapon_equip.py`。开发自测、独立审阅与
整仓回归结果以本批阶段报告为准；文档不预先宣称未执行检查通过。
