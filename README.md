# Social Downloader Control Center — Dashboard multi-bots + API + Vercel

Console web en français pour gérer plusieurs bots Telegram, leurs webhooks et leurs clés API. Le backend réutilise les cinq extracteurs de l'archive Social Media Downloader d'origine.

## Ce que fait cette version

- Dashboard protégé par mot de passe administrateur.
- Ajout de plusieurs bots Telegram en validant leur token via `getMe`.
- Enregistrement du webhook propre à chaque bot, avec secret Telegram.
- Activation / pause, renouvellement de clé API, suppression du bot et de son webhook.
- Tokens chiffrés avec Fernet. Secrets webhook chiffrés et empreintes vérifiées. Clés API stockées sous forme d'empreintes SHA-256 et affichées une seule fois.
- API `POST /v1/download` protégée par clé individuelle, utilisable par un autre programme ou bot.
- PostgreSQL persistant obligatoire : Vercel ne doit pas servir de stockage de fichiers local.

## 1. Préparer PostgreSQL

Crée une base PostgreSQL persistante (par exemple un projet Supabase avec une URL de connexion PostgreSQL). Récupère l'URL de connexion avec SSL et garde-la privée. Le programme crée automatiquement la table `managed_bots` au premier accès.

## 2. Générer les secrets

Sur ton ordinateur ou Termux, après installation de Python :

```bash
pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

La première sortie devient `FERNET_KEY`. La deuxième peut devenir `FLASK_SECRET_KEY`. Choisis également un mot de passe administrateur long et unique pour `DASHBOARD_PASSWORD`.

## 3. Déployer sur Vercel

1. Décompresse le ZIP et place le contenu dans un dépôt GitHub privé.
2. Importe ce dépôt sur Vercel. Vercel détecte l'application Flask grâce à `app.py` et `requirements.txt`.
3. Dans **Settings → Environment Variables**, ajoute :
   - `DATABASE_URL`
   - `PUBLIC_BASE_URL` (URL HTTPS finale du projet, sans slash final)
   - `DASHBOARD_PASSWORD`
   - `FLASK_SECRET_KEY`
   - `FERNET_KEY`
4. Redéploie après avoir défini les variables.
5. Ouvre `https://ton-projet.vercel.app/login` et connecte-toi.

Référence Vercel : https://vercel.com/kb/guide/ship-a-flask-app-on-vercel

## 4. Ajouter un bot Telegram

1. Dans Telegram, ouvre `@BotFather` et crée ton bot avec `/newbot`.
2. Dans le dashboard, clique **Ajouter un bot**.
3. Saisis un nom et le token reçu de BotFather.
4. Le serveur valide le token auprès de Telegram, chiffre le token, crée une clé API et un secret webhook, puis tente d'enregistrer le webhook.
5. Copie immédiatement la clé API affichée : elle ne sera plus visible ensuite.
6. Ouvre ton bot Telegram et envoie `/start`.

Le token n'est jamais nécessaire dans le navigateur après sa soumission. Ne le colle jamais dans GitHub ou un chat public. Si un token a été exposé, révoque-le via BotFather.

## 5. Utiliser l'API depuis un autre bot ou programme

Chaque bot ajouté possède sa propre clé API. L'API de téléchargement est :

`POST https://ton-projet.vercel.app/v1/download`

En-tête HTTP : `X-API-Key: sda_...`

Corps JSON :

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

Exemple Python côté serveur :

```python
import requests

response = requests.post(
    "https://ton-projet.vercel.app/v1/download",
    headers={"X-API-Key": "TA_CLE_API"},
    json={"url": "https://www.youtube.com/watch?v=VIDEO_ID"},
    timeout=60,
)
print(response.json())
```

Ne mets jamais une clé API dans le JavaScript public d'un site web ou dans une application cliente distribuée. Utilise-la côté serveur. Une rotation de clé invalide immédiatement l'ancienne.

Réponse normalisée :

```json
{
  "ok": true,
  "platform": "youtube",
  "title": "Titre du média",
  "items": [
    {"url": "https://...", "kind": "video", "quality": "720p", "title": "Titre"}
  ]
}
```

## 6. Routes

- `GET /` : état du service.
- `GET /login` : connexion administrateur.
- `GET /dashboard` : console de gestion.
- `POST /telegram/webhook/<bot_id>` : webhook isolé par bot.
- `POST /v1/download` : API externe protégée par clé.
- `GET /admin/api/bots` : liste des bots (session admin).
- `POST /admin/api/bots` : ajouter un bot (session admin + CSRF).

## 7. Limites importantes

- L'API a besoin de PostgreSQL persistant. Ne remplace pas la base par SQLite ou un fichier JSON sur Vercel : le disque des fonctions n'est pas un stockage durable.
- Le tableau de bord gère les bots dont tu possèdes le token ou que tu es autorisé à administrer. Ajouter un bot configure son webhook sur ce service ; cela ne transfère pas la propriété du bot.
- Les extracteurs Facebook, Instagram, Snapchat et Pinterest dépendent des services tiers présents dans le code d'origine. Ils peuvent changer ou devenir indisponibles. YouTube via yt-dlp peut également évoluer.
- Les fonctions Vercel ont des limites de durée et de ressources. Une extraction longue ou un gros média peut échouer ; le bot transmet un lien direct si Telegram ne peut pas récupérer le média distant.
- Les réponses Telegram et l'extraction doivent être testées en production avant d'annoncer le service comme fiable.
- N'utilise le système que pour les contenus publics que tu as le droit d'enregistrer. Il ne contourne pas les contenus privés ni les protections d'accès.

## 8. Sécurité de production

- Utilise un dépôt GitHub privé.
- Définis les cinq variables d'environnement avant le premier déploiement.
- Utilise HTTPS et un mot de passe dashboard long.
- Restreins l'accès au dashboard. Cette version utilise un mot de passe administrateur unique ; pour une exploitation multi-administrateur, ajoute un vrai fournisseur d'identité et une protection anti-bruteforce.
- Protège la base PostgreSQL et active ses sauvegardes.
- Ne partage jamais les tokens Telegram, les clés API, la clé Fernet ou l'URL complète de connexion PostgreSQL.
