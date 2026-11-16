**一轮要出多张图（mode=new）**：多张一律串行，一张跑完再跑下一张。同一动作只换视角的几张用同一个种子，第一张正常跑，其余各张加 `--reuse-seed`；想要构图不同就各用随机种子，不加 `--reuse-seed`。
每张图的提示词互不依赖（**同一时刻同一动作、仅视角不同**——正文除"视角/镜头那段"外逐字相同；该段**仅指 `prompt` 开头的镜头视角短语本身**（`prompt` 的顺序见「基本要求」），同一句里的构图约束/动作/场景/灯光一律逐字照抄，一开始就能全部想好），所以把"写 N 份 intermediate + 依次跑 N 次脚本"合进同一条 Bash 命令。
同账号可能撞并发锁（HTTP 429 Concurrent generation is locked；几只 bot 加朋友圈共用一个账号）。脚本遇到会自动排队重试（等 5/10/15/20/30 秒、共 6 次），排队约 80 秒仍撞锁就按原始那次的错误报失败。
视角由提示词决定，而床、灯光、房间、皮肤纹理等细节由 seed 主导，seed 不同则这些全部重抽，两张会像两个不同的场景。后一张要读前一张记下的 seed，所以同 seed 的几张必须串行。

```bash
W=~/resource/workspace/<agent>
G=~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py
C=~/.claude/skills/novelai-skill/assets/default_config.json
cat > "$W/im_1.json" <<'EOF'
{"mode":"new","prompt":"<画面描述>","characters":[<角色>]}
EOF
cat > "$W/im_2.json" <<'EOF'
{"mode":"new","prompt":"<画面描述>","characters":[<角色>]}
EOF
rm -f "$W"/r*.json
python3 "$G" --intermediate "$W/im_1.json" --config "$C" --ratio portrait \
  --agent-name <agent> --chat-id <chat_id> > "$W/r1.json" 2>&1; rc=$?; printf '\nexit=%s\n' "$rc" >> "$W/r1.json"
grep -q '^exit=0$' "$W/r1.json" && { python3 "$G" --intermediate "$W/im_2.json" --config "$C" --ratio portrait --reuse-seed \
  --agent-name <agent> --chat-id <chat_id> > "$W/r2.json" 2>&1; rc=$?; printf '\nexit=%s\n' "$rc" >> "$W/r2.json"; }
cat "$W/r1.json" "$W/r2.json" 2>/dev/null
```

- 构图不同的几张不带 `--reuse-seed`，彼此互不依赖，仍逐张串行运行，但后一张不以前一张成功为条件。
- 命令 timeout 按每张约 150 秒给足（含服务端 5xx 重试与撞锁排队）。
- 某一张因 HTTP 500 失败多为服务端偶发故障。同一种子的几张中第一张失败时，先补跑第一张，成功后再跑后面几张；补跑仍失败时，这一轮按失败处理，做法同各 bot 生图共享规则中的失败处理。其余情况下某一张失败时补跑一次，或本轮少发一张，已成功的照常发出。
