# ─────────────────────────────────────────────────────────────
#  Image MT5 custom — basée sur gmag11/metatrader5_vnc
#  On épingle la version exacte de l'image de base pour
#  garantir la reproductibilité même si l'upstream change.
# ─────────────────────────────────────────────────────────────

FROM gmag11/metatrader5_vnc@sha256:2fdff449cf70b74c242319828b6859592ab52dfb05690d9a989c75107dabf4c1

# Copier notre start.sh patché directement dans l'image
COPY start_patched.sh /Metatrader/start.sh
RUN chmod +x /Metatrader/start.sh

# Copier le serveur RPyC
COPY rpyc_server.py /fix/rpyc_server.py

COPY servers.dat /fix/servers.dat
COPY DataBridge.mq5 /fix/DataBridge.mq5
COPY DataBridge.ex5 /fix/DataBridge.ex5


# Copier les fichiers de config MT5 (options de trading)