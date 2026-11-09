#!/bin/sh
# 17/10: no horario do pipeline a curadora substitui a coletora no grupo (ok do Breno em 16/10). Pausa so a
# coletora (job do Hermes), pelo controle dele; a proxima janela dela religa. Usado pelo pipeline fora da agenda.
cd /home/ubuntu/.hermes/hermes-agent && HERMES_HOME=/home/ubuntu/.hermes venv/bin/python -c 'from cron.jobs import pause_job; j = pause_job("8d7d488a8520", "curadora: pipeline fora da agenda"); print("coletora:", j.get("state") if j else "nao achei")'
