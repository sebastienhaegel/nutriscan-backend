# 🚀 Guide Complet - Système d'Apprentissage Centralisé NutriScan

## 📍 Résumé exécutif

Vous déployez un système où :
- **Chaque utilisateur** scanne une photo → Claude reconnaît
- **Chaque photo** est envoyée au serveur Railway
- **Au serveur** : accumulation des données jusqu'à **50 photos par aliment**
- **À 50 photos** : réentraînement automatique d'un modèle Core ML
- **Nouveau modèle** : téléchargé par tous les utilisateurs
- **Résultat** : Core ML reconnaît mieux chaque jour, réductions coûts Claude

---

## 🏗️ Architecture complète

```
┌─────────────────────────────────────────────────────────────┐
│                    iOS App (NutriScan)                      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  1. User scanne photo de pizza                          │ │
│  │  2. Core ML.predict() → Échoue (confiance < 0.7)       │ │
│  │  3. askClaude(photo) → "Pizza"                          │ │
│  │  4. ✨ contributePhoto(photo, "pizza")                  │ │
│  └────────────────────────────────────────────────────────┘ │
└────────────────┬────────────────────────────────────────────┘
                 │ HTTP POST /api/learning/contribute
                 │ • photo_base64
                 │ • label: "pizza"
                 │ • user_id: "abc-123"
                 │ • confidence: 0.9
                 ▼
┌─────────────────────────────────────────────────────────────┐
│               Railway Backend (Python)                      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  POST /api/learning/contribute                          │ │
│  │  → Sauvegarder en PostgreSQL                            │ │
│  │  → Compter photos "pizza": 45                           │ │
│  │  → Si count % 50 == 0: trigger_retraining()             │ │
│  └────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Async: retrain_coreml_model(version=2)                 │ │
│  │  1. Backup ancien modèle en /backups/                   │ │
│  │  2. Récupérer 50+ photos "pizza" de PostgreSQL         │ │
│  │  3. Organiser en /datasets/v2/pizza/                    │ │
│  │  4. Lancer Create ML training                           │ │
│  │  5. Générer NutriScan.mlmodel                           │ │
│  │  6. Copier en /models/v2/                               │ │
│  │  7. Marquer v2 comme "ready"                            │ │
│  │  8. Push notification: "new_model_available"            │ │
│  └────────────────────────────────────────────────────────┘ │
└────────────────┬────────────────────────────────────────────┘
                 │ Polling toutes les 60s
                 │ GET /api/learning/latest_model
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                    iOS App (Update)                         │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  checkForModelUpdate()                                  │ │
│  │  → Nouvelle version v2 disponible!                      │ │
│  │  → downloadAndInstallModel(v2_base64)                   │ │
│  │  → Remplacer Core ML local                              │ │
│  │  → modelVersion = 2                                     │ │
│  │                                                          │ │
│  │  Prochain scan de pizza:                                │ │
│  │  → Core ML.predict() → "Pizza" ✅ (grâce à v2)         │ │
│  │  → Pas besoin Claude! Économie 💰                       │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 ÉTAPE 1 : Intégration Backend Railway

### 1.1 Ajouter les dépendances Python

Modifier `requirements.txt` :

```txt
fastapi==0.104.1
uvicorn==0.24.0
sqlalchemy==2.0.23
psycopg2-binary==2.9.9
python-multipart==0.0.6
python-dotenv==1.0.0

# Pour Create ML (optionnel si on simule)
# turicreate==6.4.1
```

Puis installer :

```bash
pip install -r requirements.txt --break-system-packages
```

### 1.2 Ajouter les fichiers Python

Copier dans votre projet Railway :
- `contribute_api.py` → endpoints de contribution
- `train_model.py` → logique de réentraînement

### 1.3 Intégrer dans main.py

Ajouter au début de votre `main.py` :

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contribute_api import router as learning_router

app = FastAPI()

# CORS pour iOS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inclure le router d'apprentissage
app.include_router(learning_router)

# ... reste de votre app
```

### 1.4 Initialiser base de données

Déployer sur Railway et lancer :

```bash
python -c "from contribute_api import Base, engine; Base.metadata.create_all(engine)"
```

---

## 📱 ÉTAPE 2 : Intégration App iOS

### 2.1 Ajouter les fichiers Swift

Copier dans Xcode :
- `NutriScanLearningVM.swift` → ViewModel d'apprentissage
- `AnalyserViewWithContribution.swift` → Analyser avec contribution

### 2.2 Mettre à jour ContentView

Remplacer l'onglet Analyser par :

```swift
struct ContentView: View {
    var body: some View {
        TabView {
            AnalyserView()  // ← NOUVEAU avec contribution
                .tabItem {
                    Label("Analyser", systemImage: "camera.fill")
                }
            
            // ... autres onglets
        }
    }
}
```

### 2.3 Mettre à jour l'URL backend

Dans `NutriScanLearningVM.swift`, remplacer :

```swift
let backendURL = URL(string: "https://votre-railway-url.railway.app")!
```

Par votre URL Railway (exemple : `https://web-production-c1f45.up.railway.app`)

### 2.4 Build et test

```
Cmd+B → Compiler
Cmd+R → Lancer l'app
```

---

## 🔄 FLUX COMPLET

### Scénario 1 : Première photo d'ananas (photo 1-49)

```
1. User scanne ananas
2. Core ML échoue (pas assez de données locales)
3. Claude dit "Ananas"
4. App envoie au serveur: photo + "ananas"
5. Serveur: 23 photos d'ananas total
6. Pas encore 50 → pas de réentraînement
7. User: résultat affiché, fin
```

### Scénario 2 : Photo critique (photo 50)

```
1. User 25 scanne ananas
2. Core ML échoue
3. Claude dit "Ananas"
4. App envoie au serveur: photo + "ananas"
5. Serveur: compte = 50 ananas!
6. ⚡ trigger_retraining() lancé en arrière-plan
   - Backup v1
   - Préparer dataset 50 photos ananas
   - Entraîner Core ML (Create ML)
   - Générer v2
   - Marquer v2 comme "ready"
7. Tous les users reçoivent notification
8. Téléchargement automatique v2
```

### Scénario 3 : Photo suivante (photo 51)

```
1. User 26 scanne ananas (modèle v2 installé)
2. Core ML predict() → "Ananas" ✅ (90% confiance)
3. Affichage résultat IMMÉDIAT
4. Pas d'appel Claude! 
5. Économie: $0.001 vs avant
```

---

## 📊 Monitoring et stats

### Endpoint : Voir les stats globales

```bash
curl https://votre-railway-url/api/learning/stats
```

Réponse :
```json
{
  "total_contributions": 245,
  "active_users": 12,
  "labels": [
    {"label": "pizza", "count": 52},
    {"label": "ananas", "count": 50},
    {"label": "pomme", "count": 43}
  ],
  "current_model_version": 3,
  "model_status": "ready"
}
```

### Dashboard dans l'app

L'app affiche automatiquement :
- Total contributions
- Nombre utilisateurs
- Top aliments reconnus
- Version modèle actuelle

---

## 🔐 Configuration et sécurité

### Variables d'environnement (Railway)

```
DATABASE_URL=postgresql://user:pass@localhost/nutriscan
PYTHON_VERSION=3.11
```

### Limites de sécurité

**À ajouter dans `contribute_api.py` :**

```python
# Limiter taille photo
MAX_PHOTO_SIZE = 5 * 1024 * 1024  # 5MB

# Rate limiting par user
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@router.post("/contribute")
@limiter.limit("10/minute")  # 10 contributions/minute max
async def contribute_photo(...):
    if len(photo_base64) > MAX_PHOTO_SIZE:
        raise HTTPException(413, "Photo trop grande")
```

---

## 📈 Économies escomptées

### Avant (sans apprentissage)

```
1000 users × 5 scans/jour × $0.001 = $5/jour = $150/mois
```

### Après 1 semaine

```
- Premier jour: identique ($5)
- Jour 3: 30% réductions (-$1.50) = $3.50
- Jour 7: 50% réductions = $2.50/jour
```

### Après 1 mois

```
- Modèle bien entraîné
- 80% des scans = Core ML local
- 20% = Claude (nouveaux aliments)
- Coût: $1/jour = $30/mois
- 💰 Économie: $120/mois!
```

---

## 🐛 Troubleshooting

### Problème : App envoie mais rien ne se passe

**Vérifier :**
```bash
# 1. Backend accessible?
curl https://votre-railway-url/api/learning/stats

# 2. Logs Railway
# Aller dans Railway → Logs → chercher "POST /contribute"

# 3. DB connectée?
# Dans Railway → PostgreSQL → Logs
```

### Problème : Réentraînement jamais déclenché

**Vérifier :**
```python
# Dans Railway terminal:
python -c "from sqlalchemy import create_engine, text; engine = create_engine(os.getenv('DATABASE_URL')); result = engine.execute(text('SELECT COUNT(*) FROM contributions WHERE label=\"pizza\"')); print(result.fetchone())"
```

### Problème : Modèle ne se télécharge pas

**Solution :**
```swift
// Dans NutriScanLearningVM.swift, ajouter logs
func checkForModelUpdate() async {
    do {
        let url = backendURL.appendingPathComponent("/api/learning/latest_model")
        print("🔍 Vérification modèle: \(url)")
        let (data, response) = try await URLSession.shared.data(from: url)
        print("📊 Réponse: \(response)")
        
        if let response = try? JSONDecoder().decode(ModelResponse.self, from: data) {
            print("✅ Modèle v\(response.version) trouvé!")
        }
    } catch {
        print("❌ Erreur: \(error)")
    }
}
```

---

## 🚀 Déploiement final

### Checklist

- [ ] Fichiers Python ajoutés à Railway
- [ ] Fichiers Swift ajoutés à Xcode
- [ ] URL backend mise à jour dans app
- [ ] Base de données PostgreSQL vérifiée
- [ ] App compilée et testée
- [ ] Premier scan effectué
- [ ] Photo reçue au serveur (vérifier logs)
- [ ] Stats affichées dans app
- [ ] Attendre 50 photos pour première réentraînement

### Commandes finales

```bash
# Railway
railway up

# Xcode
Cmd+K → Clean Build Folder
Cmd+B → Build
Cmd+R → Run
```

---

## 📞 Support

**Si erreur :**
1. Vérifier logs Xcode (Product → Scheme → Edit Scheme → Run → Console)
2. Vérifier logs Railway (Railway Dashboard → Logs)
3. Tester endpoint directement :
   ```bash
   curl -X GET https://votre-railway-url/api/learning/stats
   ```

---

## ✨ Prochaines améliorations

- [ ] Push notifications natif (APNs)
- [ ] Dashboard web (stats temps réel)
- [ ] Export données modèle
- [ ] A/B testing versions modèles
- [ ] Feedback utilisateur sur résultats

---

**Bon déploiement! 🎉**
