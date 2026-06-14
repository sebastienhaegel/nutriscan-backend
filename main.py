from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import json
import traceback

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    image_base64: str
    age: int
    gender: str
    weight: int
    goal: str
    poids_plat: int

class SuggestionsRequest(BaseModel):
    prompt: str

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        prompt = f"""Tu es un expert en nutrition. Analyse la photo de ce repas et réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown).

Profil : {req.gender}, {req.age} ans, {req.weight} kg, objectif: {req.goal}.
Poids total du plat servi sur la photo : {req.poids_plat} grammes.

Estime la composition de ce plat (proportions des ingrédients visibles) et calcule les macronutriments totaux pour ce poids de {req.poids_plat}g.

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

Les valeurs "calories", "proteines_g", "glucides_g", "lipides_g" doivent correspondre au poids total réel de {req.poids_plat}g, pas à 100g."""
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

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        return result

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR DÉTAILLÉE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/suggestions")
async def suggestions(req: SuggestionsRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": req.prompt
            }]
        )
        return {"result": response.content[0].text}
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR SUGGESTIONS: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/check")
def check():
    key = os.environ.get("ANTHROPIC_API_KEY", "NON TROUVÉE")
    return {
        "key_found": key != "NON TROUVÉE",
        "key_start": key[:10] if key != "NON TROUVÉE" else "NON TROUVÉE"
    }
class NextMealRequest(BaseModel):
    nom_repas: str
    score: int
    nutrients: list
    aliments_frigo: list[str] = []

@app.post("/next-meal")
async def next_meal(req: NextMealRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        nutrients_str = ", ".join([
            f"{n['nom']} à {n['pct']}%"
            for n in req.nutrients
        ])

        frigo_str = ", ".join(req.aliments_frigo) if req.aliments_frigo else "aucune donnée disponible"

        prompt = f"""Tu es un expert en nutrition. L'utilisateur vient de manger : {req.nom_repas} (score nutritionnel: {req.score}/100).

Apports de ce repas : {nutrients_str}.

Aliments disponibles dans son frigo : {frigo_str}.

En fonction de ces apports, suggère UN SEUL repas idéal pour le prochain repas. Si des aliments du frigo permettent de réaliser ce repas, utilise-les en priorité et mentionne-le.
Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{
  "nom": "Nom du repas suggéré",
  "description": "Description courte et appétissante (1-2 phrases)",
  "raison": "Pourquoi ce repas complète bien le précédent (1 phrase)",
  "ingredients": ["ingrédient 1", "ingrédient 2", "ingrédient 3", "ingrédient 4"]
}}"""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": prompt
            }]
        )

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        return result

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR NEXT MEAL: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

class ScanInventoryRequest(BaseModel):
    image_base64: str

class RecipeRequest(BaseModel):
    aliments: list[str]

@app.post("/scan-inventory")
async def scan_inventory(req: ScanInventoryRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = """Tu es un expert en analyse alimentaire. Analyse cette photo (frigo, placard ou ticket de caisse) et identifie tous les aliments visibles ou listés.

Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{
  "aliments": [
    { "nom": "Nom de l'aliment", "quantite": "ex: 2, 500g, 1L", "categorie": "Légumes" }
  ]
}

Catégories possibles : "Légumes", "Fruits", "Viandes/Poissons", "Produits laitiers", "Féculents", "Épicerie", "Boissons", "Autre".
Si c'est un ticket de caisse, liste les produits alimentaires achetés (ignore les articles non alimentaires)."""

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

        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        return result

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR SCAN INVENTORY: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recipe-from-inventory")
async def recipe_from_inventory(req: RecipeRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        aliments_str = ", ".join(req.aliments)

        prompt = f"""Tu es un chef cuisinier spécialisé dans les recettes simples et rapides du quotidien. Voici les aliments disponibles dans le frigo/placard :

{aliments_str}

Propose 3 recettes SIMPLES et RAPIDES réalisables principalement avec ces ingrédients. Réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown) :
{{
  "recettes": [
    {{
      "nom": "Nom de la recette",
      "description": "Description courte et appétissante (1-2 phrases)",
      "temps_minutes": 20,
      "ingredients_utilises": ["ingrédient 1", "ingrédient 2"],
      "ingredients_manquants": ["ingrédient à acheter"]
    }}
  ]
}}

Règles importantes :
- Maximum 5 ingrédients au total par recette (en comptant ingredients_utilises + ingredients_manquants)
- Privilégie les recettes avec peu d'étapes de préparation (moins de 20 minutes)
- Privilégie le maximum d'ingrédients déjà disponibles dans le frigo
- Évite les techniques de cuisine complexes (pas de marinades longues, pas de cuissons multiples)
- Pense "facile pour un soir de semaine" : poêlées, gratins simples, salades composées, pâtes/riz + protéine + légume"""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": prompt
            }]
        )

raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        return result

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR RECIPE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}


