# Importação automática de boletos do Gmail

O FluxoPay lê os e-mails do Gmail, encontra os boletos anexados (PDF ou foto),
confere o código de barras e salva sozinho. O que não der para ler com certeza
vai para **Financeiro → Caixa de Entrada** para você aprovar com 1 clique.

## Como o sistema decide

| Situação | O que acontece |
|---|---|
| Código válido + valor + vencimento + loja identificada | **Salvo direto** |
| Faltou loja, valor ou vencimento / código não confere | **Caixa de Entrada** (revisão) |
| Mesmo código já cadastrado | Marcado como **duplicado** (não salva de novo) |
| Nota fiscal (DANFE) ou PDF sem boleto | **Ignorado** |

* Valor e vencimento saem do próprio código de barras (matemática, sem IA).
* A loja é identificada pelo **CNPJ do pagador** impresso no boleto
  (ou pelo nome da loja, se aparecer no texto).
* Os e-mails processados recebem a etiqueta **FluxoPay** no Gmail e não são
  marcados como lidos.

## Passo 1 — CNPJ de cada loja
Financeiro → Admin → em cada loja, preencha o **CNPJ** e clique em Salvar.

## Passo 2 — Senha de app do Gmail
1. Na conta Google que recebe os boletos, ative a **Verificação em duas etapas**.
2. Acesse https://myaccount.google.com/apppasswords, crie uma senha de app
   chamada "FluxoPay" e copie os 16 caracteres.
3. Confirme que o IMAP está ativo: Gmail → Configurações → Encaminhamento e POP/IMAP.

## Passo 3 — Variáveis no Render (Environment)
| Variável | Valor |
|---|---|
| `GMAIL_USER` | o e-mail, ex.: `contas@gmail.com` |
| `GMAIL_APP_PASSWORD` | a senha de app de 16 caracteres |
| `CRON_TOKEN` | uma senha longa qualquer (protege o endereço do agendador) |
| `GEMINI_API_KEY` | *(opcional)* chave do Google AI Studio — para ler **fotos e PDFs escaneados** |
| `GEMINI_MODEL` | *(opcional)* padrão `gemini-flash-latest` |
| `IMPORT_DIAS` / `IMPORT_MAX` | *(opcional)* padrão 30 dias / 15 e-mails por rodada |

Chave do Gemini: https://aistudio.google.com/apikey → "Create API key".
A assinatura Gemini Pro do app **não** vale para a API; a chave do AI Studio
tem uso gratuito limitado, suficiente para o volume de boletos.

## Passo 4 — Rodar sozinho (agendador)
O Render gratuito "dorme"; por isso um agendador externo chama o sistema.

**Opção A — GitHub Actions (já incluído no repositório):**
GitHub → Settings → Secrets and variables → Actions → New repository secret:
`CRON_URL` = `https://fluxopay.onrender.com/tarefas/importar-emails?token=SEU_CRON_TOKEN`.
Roda a cada 30 min, das 7h às 20h, de segunda a sábado.

**Opção B — cron-job.org (grátis):** crie um job com a mesma URL a cada 30 min.

Também dá para buscar na hora pelo botão **"Buscar e-mails agora"** na Caixa de
Entrada, ou arrastar vários PDFs/fotos de uma vez para a mesma tela.
