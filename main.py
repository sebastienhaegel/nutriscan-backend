from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import anthropic
import os
import json
import traceback
import time
import uuid
import resend
from collections import defaultdict
from datetime import datetime
from contribute_api import router as learning_router
app.include_router(learning_router)
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# MARK: — Base de données PostgreSQL
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()

class PlatPartage(Base):
    __tablename__ = "plats_partages"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nom = Column(String, nullable=False, unique=True)
    calories = Column(Integer, default=0)
    proteines_g = Column(Integer, default=0)
    glucides_g = Column(Integer, default=0)
    lipides_g = Column(Integer, default=0)
    score = Column(Integer, default=0)
    verdict = Column(String, default="")
    commentaire = Column(Text, default="")
    nutrients = Column(Text, default="[]")
    conseils = Column(Text, default="[]")
    valide = Column(Boolean, default=True)
    date_creation = Column(DateTime, default=datetime.utcnow)
    nombre_utilisations = Column(Integer, default=1)

class CorrectionPending(Base):
    __tablename__ = "corrections_pending"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    plat_id = Column(String, nullable=True)
    nom_original = Column(String, nullable=False)
    nom_corrige = Column(String, nullable=False)
    calories_corrige = Column(Integer, default=0)
    proteines_corrige = Column(Integer, default=0)
    glucides_corrige = Column(Integer, default=0)
    lipides_corrige = Column(Integer, default=0)
    user_id = Column(String, nullable=False)
    statut = Column(String, default="pending")
    date_soumission = Column(DateTime, default=datetime.utcnow)

if engine:
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

# MARK: — Email (Resend)
resend.api_key = os.environ.get("RESEND_API_KEY", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

def envoyer_email_correction(correction_id: str, nom_original: str, nom_corrige: str, user_id: str):
    if not resend.api_key or not ADMIN_EMAIL:
        print("⚠️ Email non configuré")
        return
    
    lien_valider = f"https://web-production-c1f45.up.railway.app/admin/valider/{correction_id}"
    lien_rejeter = f"https://web-production-c1f45.up.railway.app/admin/rejeter/{correction_id}"
    
    try:
        resend.Emails.send({
            "from": "nutriscan@resend.dev",
            "to": ADMIN_EMAIL,
            "subject": f"NutriScan — Correction à valider : {nom_original}",
            "html": f"""
            <h2>Nouvelle correction soumise</h2>
            <p><strong>Utilisateur :</strong> {user_id[:8]}...</p>
            <p><strong>Nom original :</strong> {nom_original}</p>
            <p><strong>Nom corrigé :</strong> {nom_corrige}</p>
            <br>
            <a href="{lien_valider}" style="background:#22c55e;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;margin-right:12px">
                ✅ Valider
            </a>
            <a href="{lien_rejeter}" style="background:#ef4444;color:white;padding:12px 24px;border-radius:6px;text-decoration:none">
                ❌ Rejeter
            </a>
            """
        })
        print(f"📧 Email envoyé pour correction {correction_id}")
    except Exception as e:
        print(f"❌ Erreur email: {e}")


# MARK: — Quotas
MAX_ANALYSES_PAR_JOUR = 100
user_analyses = defaultdict(list)

def verifier_quota(user_id: str) -> dict:
    now = time.time()
    hier = now - 86400
    user_analyses[user_id] = [t for t in user_analyses[user_id] if t > hier]
    appels = len(user_analyses[user_id])
    return {
        "autorise": appels < MAX_ANALYSES_PAR_JOUR,
        "appels_aujourd_hui": appels,
        "restants": max(0, MAX_ANALYSES_PAR_JOUR - appels),
        "maximum": MAX_ANALYSES_PAR_JOUR
    }

def enregistrer_appel(user_id: str):
    user_analyses[user_id].append(time.time())


# MARK: — Modèles Pydantic
class AnalyzeRequest(BaseModel):
    image_base64: str
    age: int
    gender: str
    weight: int
    goal: str
    poids_plat: int
    nom_plat: str | None = None
    user_id: str = "anonymous"

class SuggestionsRequest(BaseModel):
    prompt: str

class NextMealRequest(BaseModel):
    nom_repas: str
    score: int
    nutrients: list
    aliments_frigo: list[str] = []

class ScanInventoryRequest(BaseModel):
    image_base64: str

class RecipeRequest(BaseModel):
    aliments: list[str]
    aliment_principal: str | None = None

class CorrectionRequest(BaseModel):
    nom_original: str
    nom_corrige: str
    calories: int
    proteines_g: int
    glucides_g: int
    lipides_g: int
    user_id: str


# MARK: — Helpers base partagée
def chercher_plat_partage(nom: str):
    if not engine:
        return None
    session = Session()
    try:
        nom_lower = nom.lower().strip()
        plats = session.query(PlatPartage).filter(PlatPartage.valide == True).all()
        
        def mots_significatifs(texte):
            mots = texte.lower().strip().split()
            return set(m[:-1] if m.endswith("s") and len(m) > 3 else m for m in mots if len(m) > 2)
        
        mots_recherche = mots_significatifs(nom_lower)
        
        for plat in plats:
            mots_plat = mots_significatifs(plat.nom)
            intersection = mots_plat.intersection(mots_recherche)
            union = max(len(mots_plat), len(mots_recherche))
            if union > 0 and len(intersection) / union >= 0.6:
                plat.nombre_utilisations += 1
                session.commit()
                print(f"✅ Trouvé dans base partagée : '{plat.nom}' pour '{nom}'")
                return plat
        return None
    except Exception as e:
        print(f"Erreur recherche plat: {e}")
        return None
    finally:
        session.close()

def sauvegarder_plat_partage(result: dict):
    if not engine:
        return
    session = Session()
    try:
        nom = result.get("nom", "")
        plats = session.query(PlatPartage).filter(PlatPartage.valide == True).all()
        
        def mots(texte):
            m = texte.lower().strip().split()
            return set(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in m if len(w) > 2)
        
        mots_nom = mots(nom)
        plat_existant = None
        for p in plats:
            inter = mots(p.nom).intersection(mots_nom)
            uni = max(len(mots(p.nom)), len(mots_nom))
            if uni > 0 and len(inter) / uni >= 0.6:
                plat_existant = p
                break
        
        if plat_existant:
            macros = result.get("macros", {})
            plat_existant.calories = macros.get("calories", plat_existant.calories)
            plat_existant.proteines_g = macros.get("proteines_g", plat_existant.proteines_g)
            plat_existant.glucides_g = macros.get("glucides_g", plat_existant.glucides_g)
            plat_existant.lipides_g = macros.get("lipides_g", plat_existant.lipides_g)
            plat_existant.score = result.get("score", plat_existant.score)
            plat_existant.nombre_utilisations += 1
            session.commit()
            print(f"🔄 Plat mis à jour dans base partagée : {nom}")
        else:
            macros = result.get("macros", {})
            nouveau = PlatPartage(
                id=str(uuid.uuid4()),
                nom=nom,
                calories=macros.get("calories", 0),
                proteines_g=macros.get("proteines_g", 0),
                glucides_g=macros.get("glucides_g", 0),
                lipides_g=macros.get("lipides_g", 0),
                score=result.get("score", 0),
                verdict=result.get("verdict", ""),
                commentaire=result.get("commentaire", ""),
                nutrients=json.dumps(result.get("nutrients", [])),
                conseils=json.dumps(result.get("conseils", [])),
                valide=True
            )
            session.add(nouveau)
            session.commit()
            print(f"💾 Nouveau plat dans base partagée : {nom}")
    except Exception as e:
        print(f"Erreur sauvegarde plat: {e}")
        session.rollback()
    finally:
        session.close()


# MARK: — Endpoints
@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        quota = verifier_quota(req.user_id)
        if not quota["autorise"]:
            raise HTTPException(status_code=429, detail={
                "message": "Quota journalier atteint",
                "restants": 0,
                "maximum": quota["maximum"]
            })

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        indication_plat = ""
        if req.nom_plat:
            indication_plat = f"\nL'application a identifié ce plat comme étant : {req.nom_plat}. Utilise ce nom si tu es d'accord, sinon corrige-le.\n"

        prompt = f"""Tu es un expert en nutrition. Analyse la photo de ce repas et réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown).

Profil : {req.gender}, {req.age} ans, {req.weight} kg, objectif: {req.goal}.
Poids total du plat servi sur la photo : {req.poids_plat} grammes.
{indication_plat}
Retourne exactement ce format JSON :
{{
  "nom": "Nom du plat identifié",
  "description": "Description courte (1-2 phrases)",
  "score": 72,
  "verdict": "Titre du bilan",
  "commentaire": "Commentaire personnalisé (2-3 phrases)",
  "macros": {{
    "calories": 650,
    "proteines_g": 35,
    "glucides_g": 70,
    "lipides_g": 22
  }},
  "nutrients": [
    {{ "nom": "Protéines", "pct": 65, "niveau": "medium" }},
    {{ "nom": "Glucides",  "pct": 85, "niveau": "good"   }},
    {{ "nom": "Lipides",   "pct": 45, "niveau": "low"    }},
    {{ "nom": "Fibres",    "pct": 30, "niveau": "low"    }},
    {{ "nom": "Vitamines", "pct": 70, "niveau": "medium" }},
    {{ "nom": "Minéraux",  "pct": 55, "niveau": "medium" }}
  ],
  "conseils": ["Conseil 1", "Conseil 2", "Conseil 3"]
}}

Les valeurs macros doivent correspondre au poids total de {req.poids_plat}g."""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": req.image_base64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        enregistrer_appel(req.user_id)

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)

        # Sauvegarde dans la base partagée
        sauvegarder_plat_partage(result)

        result["quota"] = {
            "restants": MAX_ANALYSES_PAR_JOUR - len(user_analyses[req.user_id]),
            "maximum": MAX_ANALYSES_PAR_JOUR
        }

        return result

    except HTTPException:
        raise
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR DÉTAILLÉE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plat/{nom}")
async def get_plat(nom: str):
    """Cherche un plat dans la base partagée."""
    plat = chercher_plat_partage(nom)
    if not plat:
        raise HTTPException(status_code=404, detail="Plat non trouvé")
    
    return {
        "nom": plat.nom,
        "calories": plat.calories,
        "proteines_g": plat.proteines_g,
        "glucides_g": plat.glucides_g,
        "lipides_g": plat.lipides_g,
        "score": plat.score,
        "verdict": plat.verdict,
        "commentaire": plat.commentaire,
        "nutrients": json.loads(plat.nutrients) if plat.nutrients else [],
        "conseils": json.loads(plat.conseils) if plat.conseils else [],
        "description": "Plat reconnu depuis la base partagée",
        "macros": {
            "calories": plat.calories,
            "proteines_g": plat.proteines_g,
            "glucides_g": plat.glucides_g,
            "lipides_g": plat.lipides_g
        }
    }


@app.post("/correction")
async def soumettre_correction(req: CorrectionRequest):
    """Soumet une correction utilisateur."""
    if not engine:
        raise HTTPException(status_code=503, detail="Base de données non disponible")

    session = Session()
    try:
        # Cherche le plat existant
        plat = chercher_plat_partage(req.nom_original)
        plat_id = plat.id if plat else None

        correction = CorrectionPending(
            id=str(uuid.uuid4()),
            plat_id=plat_id,
            nom_original=req.nom_original,
            nom_corrige=req.nom_corrige,
            calories_corrige=req.calories,
            proteines_corrige=req.proteines_g,
            glucides_corrige=req.glucides_g,
            lipides_corrige=req.lipides_g,
            user_id=req.user_id,
            statut="pending"
        )
        session.add(correction)
        session.commit()

        # Envoie l'email à l'admin
        envoyer_email_correction(
            correction.id,
            req.nom_original,
            req.nom_corrige,
            req.user_id
        )

        return {
            "success": True,
            "correction_id": correction.id,
            "message": "Correction soumise avec succès, merci !"
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/correction/{user_id}/{nom_original}")
async def get_correction_utilisateur(user_id: str, nom_original: str):
    """Récupère la correction en attente d'un utilisateur pour un plat."""
    if not engine:
        return {"correction": None}
    
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(
            CorrectionPending.user_id == user_id,
            CorrectionPending.nom_original == nom_original,
            CorrectionPending.statut == "pending"
        ).first()
        
        if not correction:
            return {"correction": None}
        
        return {
            "correction": {
                "nom_corrige": correction.nom_corrige,
                "calories": correction.calories_corrige,
                "proteines_g": correction.proteines_corrige,
                "glucides_g": correction.glucides_corrige,
                "lipides_g": correction.lipides_corrige
            }
        }
    finally:
        session.close()


@app.get("/admin/valider/{correction_id}", response_class=HTMLResponse)
async def valider_correction(correction_id: str):
    """Valide une correction depuis l'email admin."""
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(
            CorrectionPending.id == correction_id
        ).first()
        
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        
        if correction.statut != "pending":
            return HTMLResponse(f"<h1>ℹ️ Correction déjà traitée ({correction.statut})</h1>")
        
        # Met à jour le plat dans la base partagée
        if correction.plat_id:
            plat = session.query(PlatPartage).filter(
                PlatPartage.id == correction.plat_id
            ).first()
            if plat:
                plat.nom = correction.nom_corrige
                plat.calories = correction.calories_corrige
                plat.proteines_g = correction.proteines_corrige
                plat.glucides_g = correction.glucides_corrige
                plat.lipides_g = correction.lipides_corrige
        else:
            # Crée un nouveau plat
            nouveau = PlatPartage(
                id=str(uuid.uuid4()),
                nom=correction.nom_corrige,
                calories=correction.calories_corrige,
                proteines_g=correction.proteines_corrige,
                glucides_g=correction.glucides_corrige,
                lipides_g=correction.lipides_corrige,
                score=0,
                valide=True
            )
            session.add(nouveau)
        
        correction.statut = "validee"
        session.commit()
        
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;padding:40px;text-align:center">
            <h1>✅ Correction validée !</h1>
            <p>Le plat <strong>{correction.nom_corrige}</strong> a été mis à jour dans la base partagée.</p>
            <p style="color:gray">Tous les utilisateurs bénéficieront de cette correction.</p>
        </body></html>
        """)
    except Exception as e:
        session.rollback()
        return HTMLResponse(f"<h1>❌ Erreur : {str(e)}</h1>")
    finally:
        session.close()


@app.get("/admin/rejeter/{correction_id}", response_class=HTMLResponse)
async def rejeter_correction(correction_id: str):
    """Rejette une correction depuis l'email admin."""
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(
            CorrectionPending.id == correction_id
        ).first()
        
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        
        correction.statut = "rejetee"
        session.commit()
        
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;padding:40px;text-align:center">
            <h1>❌ Correction rejetée</h1>
            <p>La correction pour <strong>{correction.nom_original}</strong> a été rejetée.</p>
        </body></html>
        """)
    finally:
        session.close()


@app.post("/suggestions")
async def suggestions(req: SuggestionsRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": req.prompt}]
        )
        return {"result": response.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/next-meal")
async def next_meal(req: NextMealRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        nutrients_str = ", ".join([f"{n['nom']} à {n['pct']}%" for n in req.nutrients])
        frigo_str = ", ".join(req.aliments_frigo) if req.aliments_frigo else "aucune donnée disponible"

        prompt = f"""Tu es un expert en nutrition. L'utilisateur vient de manger : {req.nom_repas} (score: {req.score}/100).
Apports : {nutrients_str}.
Aliments disponibles : {frigo_str}.
Suggère UN SEUL repas idéal. Réponds UNIQUEMENT en JSON :
{{
  "nom": "Nom du repas",
  "description": "Description (1-2 phrases)",
  "raison": "Pourquoi ce repas complète le précédent",
  "ingredients": ["ingrédient 1", "ingrédient 2", "ingrédient 3"]
}}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scan-inventory")
async def scan_inventory(req: ScanInventoryRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = """Analyse cette photo et identifie tous les aliments visibles.
Réponds UNIQUEMENT en JSON :
{
  "aliments": [
    { "nom": "Nom", "quantite": "500g", "categorie": "Légumes" }
  ]
}
Catégories : "Légumes", "Fruits", "Viandes/Poissons", "Produits laitiers", "Féculents", "Épicerie", "Boissons", "Autre"."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recipe-from-inventory")
async def recipe_from_inventory(req: RecipeRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        aliments_str = ", ".join(req.aliments)
        consigne = f"\nUtilise obligatoirement : {req.aliment_principal}.\n" if req.aliment_principal else ""

        prompt = f"""Chef cuisinier spécialisé recettes simples. Aliments disponibles : {aliments_str}
{consigne}
Propose 3 recettes SIMPLES en JSON :
{{
  "recettes": [{{
    "nom": "Nom",
    "description": "Description",
    "temps_minutes": 20,
    "ingredients_utilises": ["ing1"],
    "ingredients_manquants": ["ing2"]
  }}]
}}
Règles : max 5 ingrédients, au moins 1 légume/fruit, moins de 20 minutes."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/quota/{user_id}")
def get_quota(user_id: str):
    return verifier_quota(user_id)


@app.get("/check")
def check():
    key = os.environ.get("ANTHROPIC_API_KEY", "NON TROUVÉE")
    db_ok = engine is not None
    return {
        "key_found": key != "NON TROUVÉE",
        "database_connected": db_ok
    }
@app.get("/test-email")
async def test_email():
    """Test d'envoi d'email."""
    admin = os.environ.get("ADMIN_EMAIL", "NON CONFIGURÉ")
    api_key = os.environ.get("RESEND_API_KEY", "NON CONFIGURÉ")
    
    print(f"📧 Test email vers : {admin}")
    print(f"🔑 Clé Resend : {api_key[:10]}...")
    
    if not api_key or api_key == "NON CONFIGURÉ":
        return {"error": "RESEND_API_KEY manquante"}
    
    if not admin or admin == "NON CONFIGURÉ":
        return {"error": "ADMIN_EMAIL manquant"}
    
    try:
        resend.api_key = api_key
        response = resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": admin,
            "subject": "Test NutriScan",
            "html": "<h1>Test email NutriScan ✅</h1><p>Si vous recevez cet email, la configuration est correcte !</p>"
        })
        print(f"✅ Email envoyé : {response}")
        return {"success": True, "response": str(response)}
    except Exception as e:
        print(f"❌ Erreur email : {e}")
        return {"error": str(e)}
class ScanMenuRequest(BaseModel):
    image_base64: str
    semaine: str

class AnalysePlatCantineRequest(BaseModel):
    nom_plat: str
    type_plat: str


@app.post("/scan-menu")
async def scan_menu(req: ScanMenuRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en lecture de menus de cantine scolaire.
Analyse cette photo de menu de cantine et extrais tous les plats par jour.
La semaine est : {req.semaine}

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{
  "semaine": "{req.semaine}",
  "jours": [
    {{
      "jour": "Lundi",
      "date": "2024-01-15",
      "plats": [
        {{ "nom": "Carottes râpées", "type_plat": "entree" }},
        {{ "nom": "Poulet rôti", "type_plat": "plat" }},
        {{ "nom": "Haricots verts", "type_plat": "accompagnement" }},
        {{ "nom": "Yaourt", "type_plat": "dessert" }}
      ]
    }}
  ]
}}

Types possibles : "entree", "plat", "accompagnement", "dessert", "laitage", "pain"
Inclus uniquement les jours de semaine (Lundi à Vendredi)."""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": req.image_base64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR SCAN MENU: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze-plat-cantine")
async def analyze_plat_cantine(req: AnalysePlatCantineRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en nutrition scolaire.
Estime les valeurs nutritionnelles d'une portion de cantine scolaire pour un enfant.
Plat : {req.nom_plat}
Type : {req.type_plat}

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{
  "nom": "{req.nom_plat}",
  "calories": 250,
  "proteines_g": 15,
  "glucides_g": 30,
  "lipides_g": 8,
  "score": 72,
  "verdict": "Bon apport nutritionnel",
  "conseils": ["Conseil 1", "Conseil 2"]
}}

Base-toi sur une portion standard de cantine scolaire (portion enfant)."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR ANALYSE PLAT CANTINE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/health")
def health():
    return {"status": "ok"}
