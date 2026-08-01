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
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

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

class ScanMenuRequest(BaseModel):
    image_base64: str
    semaine: str

class AnalysePlatCantineRequest(BaseModel):
    nom_plat: str
    type_plat: str

class ScanReceiptRequest(BaseModel):
    prompt: str


# MARK: — Normalisation numérique
def _to_int(valeur, defaut=0):
    """Convertit n'importe quoi en int : 72.5 -> 72, "650" -> 650, None -> defaut."""
    try:
        if valeur is None:
            return defaut
        if isinstance(valeur, bool):
            return defaut
        if isinstance(valeur, (int, float)):
            return int(round(float(valeur)))
        texte = str(valeur).strip().replace(",", ".")
        texte = re.sub(r"[^0-9.\-]", "", texte)
        return int(round(float(texte))) if texte not in ("", "-", ".") else defaut
    except Exception:
        return defaut


def normaliser_resultat(result: dict) -> dict:
    """Garantit que tous les champs numériques sont des entiers (Swift attend des Int)."""
    if not isinstance(result, dict):
        return result

    result["score"] = _to_int(result.get("score"))
    result["nom"] = str(result.get("nom", "")).strip()
    result["description"] = str(result.get("description", "")).strip()
    result["verdict"] = str(result.get("verdict", "")).strip()
    result["commentaire"] = str(result.get("commentaire", "")).strip()

    macros = result.get("macros") or {}
    result["macros"] = {
        "calories": _to_int(macros.get("calories")),
        "proteines_g": _to_int(macros.get("proteines_g")),
        "glucides_g": _to_int(macros.get("glucides_g")),
        "lipides_g": _to_int(macros.get("lipides_g")),
    }

    nutrients = result.get("nutrients") or []
    result["nutrients"] = [
        {
            "nom": str(n.get("nom", "")).strip(),
            "pct": _to_int(n.get("pct")),
            "niveau": str(n.get("niveau", "medium")).strip(),
        }
        for n in nutrients
        if isinstance(n, dict)
    ]

    conseils = result.get("conseils") or []
    result["conseils"] = [str(c).strip() for c in conseils if str(c).strip()]

    return result


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
            union = min(len(mots_plat), len(mots_recherche))
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
            uni = min(len(mots(p.nom)), len(mots_nom))
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


# MARK: — Parsing robuste des réponses Claude
def parser_json_claude(response, defaut=None, contexte=""):
    """Extrait un JSON d'une réponse Claude, même tronquée ou entourée de markdown.

    Renvoie `defaut` (ou {}) si rien d'exploitable n'est trouvé.
    """
    defaut = {} if defaut is None else defaut

    try:
        raw = response.content[0].text
    except Exception:
        print(f"[{contexte}] ❌ Réponse vide")
        return defaut

    tronque = getattr(response, "stop_reason", None) == "max_tokens"
    if tronque:
        print(f"[{contexte}] ⚠️ Réponse tronquée par max_tokens ({len(raw)} chars)")

    clean = raw.replace("```json", "").replace("```", "").strip()

    # Guillemets typographiques éventuels
    clean = clean.replace("\u201c", '"').replace("\u201d", '"')

    # Objet {...} ou tableau [...] : on prend ce qui commence en premier
    debut_obj = clean.find("{")
    debut_arr = clean.find("[")
    if debut_obj == -1 and debut_arr == -1:
        print(f"[{contexte}] ❌ Aucun JSON détecté")
        return defaut

    if debut_arr != -1 and (debut_obj == -1 or debut_arr < debut_obj):
        ouvrant, fermant = "[", "]"
        debut = debut_arr
    else:
        ouvrant, fermant = "{", "}"
        debut = debut_obj

    clean = clean[debut:]
    fin = clean.rfind(fermant)
    if fin != -1:
        clean = clean[:fin + 1]

    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"[{contexte}] ⚠️ Parse échoué ({e}) — réparation…")

    # Réparation 1 : virgules traînantes
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", clean))
    except json.JSONDecodeError:
        pass

    # Réparation 2 : ne garder que les objets complets (cas de troncature)
    objets = re.findall(r"\{[^{}]*\}", clean)
    if objets:
        try:
            liste = json.loads("[" + ",".join(objets) + "]")
            print(f"[{contexte}] ✅ Réparé : {len(objets)} objet(s) récupéré(s)")
            if ouvrant == "[":
                return liste
            # On tente de replacer la liste sous sa clé d'origine
            cle = re.search(r'"(\w+)"\s*:\s*\[', clean)
            if cle:
                return {cle.group(1): liste}
            return liste
        except json.JSONDecodeError:
            pass

    print(f"[{contexte}] ❌ Réparation impossible")
    return defaut


# MARK: — Endpoints
@app.get("/")
def root():
    return {"message": "NutriScan API v1", "status": "running"}

@app.get("/health")
def health():
    return {"status": "healthy", "service": "nutriscan", "version": "1.0.0"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    """Politique de confidentialité — URL requise par App Store Connect."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NutriScan — Politique de confidentialité</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       max-width:720px;margin:0 auto;padding:32px 20px;line-height:1.65;color:#1c1c1e}
  h1{font-size:1.7rem;margin-bottom:.2em}
  h2{font-size:1.15rem;margin-top:2em;color:#166534}
  .maj{color:#6b7280;font-size:.9rem;margin-top:0}
  ul{padding-left:1.2em}
  li{margin-bottom:.4em}
  code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.9em}
  footer{margin-top:3em;padding-top:1em;border-top:1px solid #e5e7eb;
         color:#6b7280;font-size:.9rem}
</style>
</head>
<body>

<h1>Politique de confidentialité — NutriScan</h1>
<p class="maj">Dernière mise à jour : 26 juillet 2026</p>

<p>NutriScan analyse des photos de repas pour en estimer la valeur
nutritionnelle. Cette page décrit les données traitées et leur destination.</p>

<h2>Données conservées sur votre appareil</h2>
<p>Les informations suivantes restent stockées localement sur votre iPhone et
ne sont transmises à aucun serveur :</p>
<ul>
  <li>Les profils familiaux : prénom, âge, poids, sexe, objectif, photo</li>
  <li>L'historique des repas et les portions attribuées à chaque profil</li>
  <li>L'inventaire du réfrigérateur et les menus de cantine</li>
  <li>Votre base de plats personnelle</li>
</ul>
<p>La suppression de l'application efface définitivement ces données.
Aucune sauvegarde n'en est conservée de notre côté.</p>

<h2>Données transmises lors d'une analyse</h2>
<p>Quand vous analysez un repas, sont envoyés à notre serveur puis à
l'API Claude d'Anthropic :</p>
<ul>
  <li>La photo du repas</li>
  <li>L'âge, le poids, le sexe et l'objectif nutritionnel du profil actif</li>
  <li>Le poids estimé du plat et, le cas échéant, le nom que vous avez saisi</li>
  <li>Un identifiant anonyme servant à limiter le nombre d'analyses
      quotidiennes — il ne permet pas de vous identifier</li>
</ul>
<p>Anthropic traite ces données pour produire l'analyse. Consultez leur
politique de confidentialité sur <code>anthropic.com/privacy</code>.</p>

<h2>Base de plats partagée</h2>
<p>Les résultats d'analyse (nom du plat, calories, macronutriments, score)
sont enregistrés dans une base commune à tous les utilisateurs, afin
d'éviter de réanalyser un plat déjà connu. Cette base ne contient
<strong>ni photo, ni donnée de profil, ni identifiant</strong> — uniquement
des informations nutritionnelles sur des plats.</p>

<h2>Amélioration de la reconnaissance</h2>
<p>Les photos analysées peuvent être transmises à notre système
d'apprentissage, accompagnées du nom du plat, pour améliorer la
reconnaissance automatique. Ces photos ne sont associées à aucun profil
ni à aucun identifiant utilisateur.</p>

<h2>Corrections</h2>
<p>Si vous corrigez les valeurs nutritionnelles d'un plat, la correction
est transmise par courriel à l'administrateur pour validation. Elle
contient le nom du plat, les valeurs corrigées et un identifiant anonyme.</p>

<h2>Ce que nous ne faisons pas</h2>
<ul>
  <li>Aucune publicité, aucun traceur publicitaire</li>
  <li>Aucune revente ni partage commercial de données</li>
  <li>Aucun compte utilisateur, aucun mot de passe collecté</li>
  <li>Aucune géolocalisation</li>
</ul>

<h2>Conservation</h2>
<p>Les données de profil et l'historique demeurent sur votre appareil aussi
longtemps que l'application y est installée. Les informations
nutritionnelles de la base partagée sont conservées sans limite de durée,
n'étant rattachées à aucune personne.</p>

<h2>Vos droits</h2>
<p>Conformément au Règlement général sur la protection des données (RGPD),
vous disposez d'un droit d'accès, de rectification, d'effacement et
d'opposition. Les données locales s'effacent depuis l'application
(historique) ou en la désinstallant. Pour toute autre demande,
écrivez-nous à l'adresse ci-dessous.</p>

<h2>Enfants</h2>
<p>L'application permet de créer des profils pour des enfants, gérés par un
adulte. Les données de ces profils restent sur l'appareil et ne sont
transmises que dans le cadre décrit plus haut.</p>

<h2>Modifications</h2>
<p>Cette politique peut évoluer. La date en tête de page indique la
dernière révision.</p>

<h2>Contact</h2>
<p>Pour toute question relative à vos données : <code>haegel.s@hotmail.fr</code></p>

<footer>NutriScan — application indépendante</footer>

</body>
</html>""")


@app.get("/support", response_class=HTMLResponse)
def support():
    """Page d'assistance — URL requise par App Store Connect."""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NutriScan — Assistance</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       max-width:720px;margin:0 auto;padding:32px 20px;line-height:1.65;color:#1c1c1e}
  h1{font-size:1.7rem;margin-bottom:.1em}
  .sous{color:#6b7280;margin-top:0}
  h2{font-size:1.15rem;margin-top:2em;color:#166534}
  .q{font-weight:600;margin-bottom:.2em;margin-top:1.2em}
  .contact{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;
           padding:18px 20px;margin:2em 0}
  code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.9em}
  a{color:#166534}
  footer{margin-top:3em;padding-top:1em;border-top:1px solid #e5e7eb;
         color:#6b7280;font-size:.9rem}
</style>
</head>
<body>

<h1>Assistance NutriScan</h1>
<p class="sous">Aide et contact</p>

<div class="contact">
  <strong>Une question, un problème ?</strong><br>
  Écrivez à <a href="mailto:haegel.s@hotmail.fr">haegel.s@hotmail.fr</a><br>
  <span style="color:#6b7280;font-size:.9rem">Réponse sous quelques jours.</span>
</div>

<h2>Questions fréquentes</h2>

<p class="q">Comment analyser un repas ?</p>
<p>Depuis l'onglet Analyser, prenez une photo ou choisissez-en une dans
votre galerie. Saisissez le nom du plat si vous le connaissez : l'analyse
sera nettement plus précise. Ajustez le poids estimé, puis lancez
l'analyse.</p>

<p class="q">Pourquoi le poids du plat est-il important ?</p>
<p>Toutes les valeurs nutritionnelles en découlent, ainsi que le calcul des
portions lorsqu'un plat est partagé. Un poids erroné fausse l'ensemble des
résultats.</p>

<p class="q">Comment partager un plat entre plusieurs personnes ?</p>
<p>Dans le calendrier, appuyez sur un repas. Indiquez la quantité en grammes
consommée par chaque membre. Les calories sont réparties proportionnellement
et apparaissent dans le calendrier de chacun.</p>

<p class="q">Comment ajouter un membre de la famille ?</p>
<p>Appuyez sur le sélecteur de profil, en haut de l'onglet Analyser. Vous
pouvez y créer, modifier ou supprimer des profils.</p>

<p class="q">Mes anciens repas affichent 0 kcal</p>
<p>Les repas enregistrés avant la mise à jour ne comportaient pas de données
caloriques. Seuls les repas analysés depuis affichent leurs valeurs.</p>

<p class="q">L'analyse échoue ou renvoie une erreur</p>
<p>Vérifiez votre connexion : l'analyse nécessite Internet. Le nombre
d'analyses est également limité chaque jour ; le compteur restant s'affiche
sous la photo.</p>

<p class="q">Comment scanner un ticket de caisse ?</p>
<p>Dans l'onglet Frigo, choisissez l'option PDF et sélectionnez le fichier
de votre ticket. Les produits alimentaires sont ajoutés automatiquement à
l'inventaire.</p>

<p class="q">Puis-je supprimer mes données ?</p>
<p>Le bouton corbeille du calendrier efface l'historique. Désinstaller
l'application supprime définitivement toutes les données locales.</p>

<h2>Signaler une erreur nutritionnelle</h2>
<p>Si les valeurs d'un plat vous semblent inexactes, utilisez le bouton
« Corriger ce plat » sous le résultat d'analyse. Les corrections sont
examinées avant d'être appliquées.</p>

<h2>Confidentialité</h2>
<p>Consultez la <a href="/privacy">politique de confidentialité</a>.</p>

<footer>NutriScan — application indépendante</footer>

</body>
</html>""")


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        quota = verifier_quota(req.user_id)
        if not quota["autorise"]:
            raise HTTPException(status_code=429, detail={"message": "Quota journalier atteint", "restants": 0, "maximum": quota["maximum"]})
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        indication_plat = ""
        if req.nom_plat:
            indication_plat = f"\nL'application a identifié ce plat comme étant : {req.nom_plat}. Utilise ce nom si tu es d'accord, sinon corrige-le.\n"
        prompt = f"""Tu es un expert en nutrition. Analyse la photo de ce repas et réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown).
Profil : {req.gender}, {req.age} ans, {req.weight} kg, objectif: {req.goal}.
Poids total du plat servi sur la photo : {req.poids_plat} grammes.
{indication_plat}
Retourne exactement ce format JSON :
{{"nom": "Nom du plat identifié", "description": "Description courte (1-2 phrases)", "score": 72, "verdict": "Titre du bilan", "commentaire": "Commentaire personnalisé (2-3 phrases)", "macros": {{"calories": 650, "proteines_g": 35, "glucides_g": 70, "lipides_g": 22}}, "nutrients": [{{"nom": "Protéines", "pct": 65, "niveau": "medium"}}, {{"nom": "Glucides", "pct": 85, "niveau": "good"}}, {{"nom": "Lipides", "pct": 45, "niveau": "low"}}, {{"nom": "Fibres", "pct": 30, "niveau": "low"}}, {{"nom": "Vitamines", "pct": 70, "niveau": "medium"}}, {{"nom": "Minéraux", "pct": 55, "niveau": "medium"}}], "conseils": ["Conseil 1", "Conseil 2", "Conseil 3"]}}
Les valeurs macros doivent correspondre au poids total de {req.poids_plat}g."""
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        enregistrer_appel(req.user_id)
        result = parser_json_claude(response, defaut={}, contexte="analyze")
        if not result:
            raise HTTPException(status_code=502, detail="Réponse IA illisible, réessayez")
        result = normaliser_resultat(result)   # ✅ force les entiers pour Swift
        sauvegarder_plat_partage(result)
        result["quota"] = {"restants": MAX_ANALYSES_PAR_JOUR - len(user_analyses[req.user_id]), "maximum": MAX_ANALYSES_PAR_JOUR}
        return result
    except HTTPException:
        raise
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR DÉTAILLÉE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/plat/{nom}")
async def get_plat(nom: str):
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
        "macros": {"calories": plat.calories, "proteines_g": plat.proteines_g, "glucides_g": plat.glucides_g, "lipides_g": plat.lipides_g}
    }

@app.post("/correction")
async def soumettre_correction(req: CorrectionRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Base de données non disponible")
    session = Session()
    try:
        plat = chercher_plat_partage(req.nom_original)
        plat_id = plat.id if plat else None
        correction = CorrectionPending(id=str(uuid.uuid4()), plat_id=plat_id, nom_original=req.nom_original, nom_corrige=req.nom_corrige, calories_corrige=req.calories, proteines_corrige=req.proteines_g, glucides_corrige=req.glucides_g, lipides_corrige=req.lipides_g, user_id=req.user_id, statut="pending")
        session.add(correction)
        session.commit()
        envoyer_email_correction(correction.id, req.nom_original, req.nom_corrige, req.user_id)
        return {"success": True, "correction_id": correction.id, "message": "Correction soumise avec succès, merci !"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/correction/{user_id}/{nom_original}")
async def get_correction_utilisateur(user_id: str, nom_original: str):
    if not engine:
        return {"correction": None}
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.user_id == user_id, CorrectionPending.nom_original == nom_original, CorrectionPending.statut == "pending").first()
        if not correction:
            return {"correction": None}
        return {"correction": {"nom_corrige": correction.nom_corrige, "calories": correction.calories_corrige, "proteines_g": correction.proteines_corrige, "glucides_g": correction.glucides_corrige, "lipides_g": correction.lipides_corrige}}
    finally:
        session.close()

@app.get("/admin/valider/{correction_id}", response_class=HTMLResponse)
async def valider_correction(correction_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.id == correction_id).first()
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        if correction.statut != "pending":
            return HTMLResponse(f"<h1>ℹ️ Correction déjà traitée ({correction.statut})</h1>")
        if correction.plat_id:
            plat = session.query(PlatPartage).filter(PlatPartage.id == correction.plat_id).first()
            if plat:
                plat.nom = correction.nom_corrige
                plat.calories = correction.calories_corrige
                plat.proteines_g = correction.proteines_corrige
                plat.glucides_g = correction.glucides_corrige
                plat.lipides_g = correction.lipides_corrige
        else:
            nouveau = PlatPartage(id=str(uuid.uuid4()), nom=correction.nom_corrige, calories=correction.calories_corrige, proteines_g=correction.proteines_corrige, glucides_g=correction.glucides_corrige, lipides_g=correction.lipides_corrige, score=0, valide=True)
            session.add(nouveau)
        correction.statut = "validee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h1>✅ Correction validée !</h1><p>Le plat <strong>{correction.nom_corrige}</strong> a été mis à jour dans la base partagée.</p><p style="color:gray">Tous les utilisateurs bénéficieront de cette correction.</p></body></html>""")
    except Exception as e:
        session.rollback()
        return HTMLResponse(f"<h1>❌ Erreur : {str(e)}</h1>")
    finally:
        session.close()

@app.get("/admin/rejeter/{correction_id}", response_class=HTMLResponse)
async def rejeter_correction(correction_id: str):
    if not engine:
        return HTMLResponse("<h1>Base de données non disponible</h1>")
    session = Session()
    try:
        correction = session.query(CorrectionPending).filter(CorrectionPending.id == correction_id).first()
        if not correction:
            return HTMLResponse("<h1>❌ Correction introuvable</h1>")
        correction.statut = "rejetee"
        session.commit()
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h1>❌ Correction rejetée</h1><p>La correction pour <strong>{correction.nom_original}</strong> a été rejetée.</p></body></html>""")
    finally:
        session.close()

@app.post("/suggestions")
async def suggestions(req: SuggestionsRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2000, messages=[{"role": "user", "content": req.prompt}])
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
{{"nom": "Nom du repas", "description": "Description (1-2 phrases)", "raison": "Pourquoi ce repas complète le précédent", "ingredients": ["ingrédient 1", "ingrédient 2", "ingrédient 3"]}}"""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={}, contexte="next-meal")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-inventory")
async def scan_inventory(req: ScanInventoryRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = """Analyse cette photo et identifie tous les aliments visibles.
Réponds UNIQUEMENT en JSON :
{"aliments": [{ "nom": "Nom", "quantite": "500g", "categorie": "Légumes" }]}
Catégories : "Légumes", "Fruits", "Viandes/Poissons", "Produits laitiers", "Féculents", "Épicerie", "Boissons", "Autre"."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        return parser_json_claude(response, defaut={"aliments": []}, contexte="scan-inventory")
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
{{"recettes": [{{"nom": "Nom", "description": "Description", "temps_minutes": 20, "ingredients_utilises": ["ing1"], "ingredients_manquants": ["ing2"]}}]}}
Règles : max 5 ingrédients, au moins 1 légume/fruit, moins de 20 minutes."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2048, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={"recettes": []}, contexte="recipe")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-menu")
async def scan_menu(req: ScanMenuRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en lecture de menus de cantine scolaire.
Analyse cette photo de menu de cantine et extrais tous les plats par jour.
La semaine est : {req.semaine}
Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"semaine": "{req.semaine}", "jours": [{{"jour": "Lundi", "date": "2024-01-15", "plats": [{{"nom": "Carottes râpées", "type_plat": "entree"}}, {{"nom": "Poulet rôti", "type_plat": "plat"}}, {{"nom": "Haricots verts", "type_plat": "accompagnement"}}, {{"nom": "Yaourt", "type_plat": "dessert"}}]}}]}}
Types possibles : "entree", "plat", "accompagnement", "dessert", "laitage", "pain"
Inclus uniquement les jours de semaine (Lundi à Vendredi)."""
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_base64}}, {"type": "text", "text": prompt}]}])
        return parser_json_claude(response, defaut={"semaine": req.semaine, "jours": []}, contexte="scan-menu")
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
{{"nom": "{req.nom_plat}", "calories": 250, "proteines_g": 15, "glucides_g": 30, "lipides_g": 8, "score": 72, "verdict": "Bon apport nutritionnel", "conseils": ["Conseil 1", "Conseil 2"]}}
Base-toi sur une portion standard de cantine scolaire (portion enfant)."""
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
        return parser_json_claude(response, defaut={}, contexte="plat-cantine")
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR ANALYSE PLAT CANTINE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scan-receipt")
async def scan_receipt(req: ScanReceiptRequest):
    """Analyse un ticket de caisse via Claude"""
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = req.prompt if req.prompt else """Analyse ce ticket de caisse et retourne UNIQUEMENT:
[{"nom": "Produit", "quantite": "100g", "categorie": "Legumes"}]"""
        
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,  # ✅ 1024 était trop court : le JSON était tronqué
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw = response.content[0].text
        print(f"📄 Réponse brute: {len(raw)} chars (stop: {response.stop_reason})")
        if response.stop_reason == "max_tokens":
            print("⚠️ Réponse tronquée par max_tokens — réparation nécessaire")
        
        # Remove markdown
        clean = raw.replace("```json", "").replace("```", "").strip()
        
        # Replace smart quotes and dangerous chars
        clean = clean.replace(""", '"').replace(""", '"')
        clean = clean.replace("'", "'").replace("'", "'")
        clean = clean.replace("&amp;", "and")
        clean = clean.replace("&", "and")
        clean = clean.replace("'", "")
        
        # Extract the array [...] — même si le ] final manque (troncature)
        start = clean.find("[")
        if start == -1:
            print("❌ Aucun tableau trouvé")
            return {"aliments": []}
        
        clean = clean[start:]
        end = clean.rfind("]")
        if end != -1:
            clean = clean[:end + 1]
        print(f"✅ Tableau extrait: {len(clean)} chars")
        
        # Parse as array
        aliments = None
        try:
            aliments = json.loads(clean)
        except Exception as e:
            print(f"⚠️ Parse échoué ({e}) — tentative de réparation…")
            # ✅ RÉPARATION : garder uniquement les objets {...} complets
            objets = re.findall(r'\{[^{}]*\}', clean)
            if objets:
                repare = "[" + ",".join(objets) + "]"
                try:
                    aliments = json.loads(repare)
                    print(f"✅ Réparé : {len(objets)} objets complets récupérés")
                except Exception as e2:
                    print(f"❌ Réparation échouée : {e2}")
        
        if aliments is None:
            return {"aliments": []}
        
        # Ensure it's a list
        if not isinstance(aliments, list):
            aliments = []
        
        result = {
            "aliments": [
                {
                    "nom": str(item.get("nom", "")).strip(),
                    "quantite": str(item.get("quantite", "")).strip(),
                    "categorie": str(item.get("categorie", "")).strip()
                }
                for item in aliments
                if isinstance(item, dict) and item.get("nom")
            ]
        }
        
        print(f"✅ Result: {len(result['aliments'])} items found")
        return result
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return {"aliments": []}

@app.get("/quota/{user_id}")
def get_quota(user_id: str):
    return verifier_quota(user_id)

@app.get("/check")
def check():
    key = os.environ.get("ANTHROPIC_API_KEY", "NON TROUVÉE")
    db_ok = engine is not None
    return {"key_found": key != "NON TROUVÉE", "database_connected": db_ok}

@app.get("/test-email")
async def test_email():
    admin = os.environ.get("ADMIN_EMAIL", "NON CONFIGURÉ")
    api_key = os.environ.get("RESEND_API_KEY", "NON CONFIGURÉ")
    if not api_key or api_key == "NON CONFIGURÉ":
        return {"error": "RESEND_API_KEY manquante"}
    if not admin or admin == "NON CONFIGURÉ":
        return {"error": "ADMIN_EMAIL manquant"}
    try:
        resend.api_key = api_key
        response = resend.Emails.send({"from": "onboarding@resend.dev", "to": admin, "subject": "Test NutriScan", "html": "<h1>Test email NutriScan ✅</h1><p>Si vous recevez cet email, la configuration est correcte !</p>"})
        return {"success": True, "response": str(response)}
    except Exception as e:
        return {"error": str(e)}


class ScoreAliment(Base):
    """Score nutritionnel d'un aliment Ciqual, pour 100 g.

    Clé = alim_code de l'Anses : exact et stable, contrairement au
    rapprochement flou sur les mots qui confondrait « Pomme » et
    « Pomme de terre ». Table distincte de plats_partages, qui contient
    des plats composés dont les macros portent sur une portion entière.
    """
    __tablename__ = "scores_aliments"
    source_code = Column(String, primary_key=True)
    nom = Column(String, nullable=False)
    score = Column(Integer, default=0)
    verdict = Column(String, default="")
    commentaire = Column(Text, default="")
    conseils = Column(Text, default="[]")
    date_creation = Column(DateTime, default=datetime.utcnow)
    nombre_demandes = Column(Integer, default=1)


if engine:
    Base.metadata.create_all(engine)


class ScoreAlimentRequest(BaseModel):
    source_code: str
    nom: str
    calories: int = 0
    proteines_g: int = 0
    glucides_g: int = 0
    lipides_g: int = 0
    fibres_g: int = 0


@app.post("/score-aliment")
async def score_aliment(req: ScoreAlimentRequest, force: bool = False):
    """Note un aliment sur 100 g. Claude n'est appelé qu'une fois par
    aliment, tous utilisateurs confondus : le score suivant vient du cache.
    """
    defaut = {"score": 0, "verdict": "", "commentaire": "", "conseils": [], "cache": False}

    if not req.source_code:
        raise HTTPException(status_code=400, detail="source_code requis")

    # ---- 1. Le cache -------------------------------------------------
    if engine and not force:
        session = Session()
        try:
            connu = session.query(ScoreAliment).filter(
                ScoreAliment.source_code == req.source_code).first()
            if connu:
                connu.nombre_demandes += 1
                session.commit()
                print(f"[score-aliment] cache : {connu.nom} = {connu.score}")
                return {
                    "score": connu.score,
                    "verdict": connu.verdict,
                    "commentaire": connu.commentaire,
                    "conseils": json.loads(connu.conseils) if connu.conseils else [],
                    "cache": True,
                }
        except Exception as e:
            print(f"[score-aliment] lecture cache impossible : {e}")
        finally:
            session.close()

    # ---- 2. Claude ---------------------------------------------------
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = f"""Tu es un expert en nutrition. Note la qualité nutritionnelle
de cet aliment brut, pour 100 grammes. La note porte sur l'aliment lui-même,
sa densité nutritionnelle — pas sur la quantité consommée.

Aliment : {req.nom}
Pour 100 g : {req.calories} kcal, {req.proteines_g} g de protéines,
{req.glucides_g} g de glucides, {req.lipides_g} g de lipides,
{req.fibres_g} g de fibres.

Ces chiffres viennent de la table Ciqual de l'Anses et ne couvrent que
les macronutriments. Ne pénalise PAS l'aliment pour les données absentes
et ne commente pas leur absence : appuie-toi sur ta connaissance de cet
aliment pour les vitamines, minéraux et le degré de transformation.
Une valeur à 0 peut signifier « non renseigné » et non « absent ».

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{"score": 85, "verdict": "Excellent choix", "commentaire": "Deux phrases sur l'intérêt nutritionnel de cet aliment.", "conseils": ["Conseil 1", "Conseil 2"]}}

Le score va de 0 à 100. Un aliment brut peu transformé et riche en
micronutriments mérite une note élevée même s'il est calorique."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        resultat = parser_json_claude(response, defaut=dict(defaut), contexte="score-aliment")
    except Exception as e:
        print(f"[score-aliment] Claude indisponible : {e}")
        raise HTTPException(status_code=502, detail="Notation indisponible")

    resultat["score"] = _to_int(resultat.get("score"))
    resultat["verdict"] = str(resultat.get("verdict", "")).strip()
    resultat["commentaire"] = str(resultat.get("commentaire", "")).strip()
    resultat["conseils"] = [str(c).strip() for c in (resultat.get("conseils") or []) if str(c).strip()]

    if not resultat["score"]:
        raise HTTPException(status_code=502, detail="Réponse IA illisible")

    # ---- 3. Mémoriser pour tout le monde -----------------------------
    if engine:
        session = Session()
        try:
            session.merge(ScoreAliment(
                source_code=req.source_code,
                nom=req.nom,
                score=resultat["score"],
                verdict=resultat["verdict"],
                commentaire=resultat["commentaire"],
                conseils=json.dumps(resultat["conseils"], ensure_ascii=False),
                nombre_demandes=1,
            ))
            session.commit()
            print(f"[score-aliment] mémorisé : {req.nom} = {resultat['score']}")
        except Exception as e:
            session.rollback()
            print(f"[score-aliment] écriture impossible : {e}")
        finally:
            session.close()

    resultat["cache"] = False
    return resultat

