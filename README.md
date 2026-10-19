# Fantasy bot — Boom Shakalaka

Alertas no WhatsApp para o King Coopa (ESPN Fantasy Basketball, liga 41791233).

## Alertas (v1)
- 🚑 Qualquer mudança de status de um jogador seu
- 🚨 Jogador bom (≥ `STAR_MIN_FP`) fica OUT e um companheiro de time está livre → sugere quem adicionar e quem dropar
- 🔁 Alguém dropou um jogador melhor que o seu pior jogador → lembrete de claim (waiver de 1 dia)
- 🔄 Troca/dispensa na NBA de jogador relevante, com quem ganha minutos no time antigo

Ajuste os limites em `config.py`.

## Como roda
cron-job.org → GitHub Actions (`workflow_dispatch`) a cada poucos minutos → `bot.py` lê a ESPN, compara com `state.json`, manda WhatsApp via CallMeBot e salva o novo `state.json`. O agendador do próprio GitHub roda a cada 15 min como backup.

## Secrets (Settings → Secrets and variables → Actions)
| Nome | Valor |
|---|---|
| `ESPN_S2` | cookie `espn_s2` |
| `ESPN_SWID` | cookie `SWID`, com as chaves `{}` |
| `CALLMEBOT_PHONE` | seu WhatsApp com código do país, ex. `+5511...` |
| `CALLMEBOT_APIKEY` | chave recebida do CallMeBot |

## Testes
`python -m pytest -q`
