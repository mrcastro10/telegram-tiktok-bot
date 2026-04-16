# Telegram TikTok Bot MVP

MVP en Python pour:
- recevoir un lien TikTok public dans Telegram
- passer par une page de déblocage / sponsor
- retourner le fichier si la vidéo est accessible

## Variables d'environnement

- BOT_TOKEN=token du bot depuis BotFather
- WEBHOOK_SECRET=chaine secrete longue
- APP_BASE_URL=https://ton-service.onrender.com
- BOT_USERNAME=nom_utilisateur_du_bot_sans_arobase
- FORCE_GATE=1

## Déploiement local

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

## Déploiement Render

1. Crée un repo GitHub
2. Mets ces fichiers dedans
3. Sur Render: New + Web Service
4. Connecte le repo
5. Build command:
   `pip install -r requirements.txt`
6. Start command:
   `uvicorn app:app --host 0.0.0.0 --port $PORT`
7. Ajoute les variables d'environnement

## Définir le webhook Telegram

Ouvre dans le navigateur:

```text
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://TON-DOMAINE/webhook/<WEBHOOK_SECRET>
```

## Remarques

- Ce MVP est volontairement simple.
- Pour la production, ajoute:
  - base de données
  - file d'attente
  - limitation anti-spam
  - journalisation
  - contrôle de taille de fichier
  - conformité légale / droits sur les contenus
