"""
Swift ViewModel pour le système d'apprentissage centralisé
Gère l'envoi de photos au backend Railway
"""

import SwiftUI
import Combine

class NutriScanLearningViewModel: ObservableObject {
    
    @Published var isContributing = false
    @Published var contributionMessage = ""
    @Published var modelVersion: Int = 1
    @Published var learningStats: LearningStats?
    @Published var modelUpdateAvailable = false
    
    // Configuration
    let backendURL = URL(string: "https://web-production-c1f45.up.railway.app")!
    let userID: String = UIDevice.current.identifierForVendor?.uuidString ?? "unknown"
    
    private let decoder = JSONDecoder()
    private var updateCheckTimer: Timer?
    
    // MARK: - Initialisation
    
    init() {
        // Charger version modèle sauvegardée
        if let saved = UserDefaults.standard.value(forKey: "modelVersion") as? Int {
            self.modelVersion = saved
        }
        
        // Vérifier mise à jour modèle toutes les minutes
        startModelUpdateCheck()
    }
    
    deinit {
        updateCheckTimer?.invalidate()
    }
    
    
    // MARK: - 1. CONTRIBUER UNE PHOTO
    
    func contributePhoto(
        imageData: Data,
        label: String,
        claudeConfidence: Float = 0.9
    ) async {
        """
        Envoie une photo au serveur central pour apprentissage
        
        - imageData: Image en format JPEG
        - label: Label reconnu par Claude ("ananas", "pomme", etc.)
        - claudeConfidence: Confiance de Claude (0-1)
        """
        
        DispatchQueue.main.async {
            self.isContributing = true
            self.contributionMessage = "Contribution en cours..."
        }
        
        do {
            // Encoder image en base64
            let imageBase64 = imageData.base64EncodedString()
            
            // Préparer requête
            var request = URLRequest(url: backendURL.appendingPathComponent("/api/learning/contribute"))
            request.httpMethod = "POST"
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            
            // Créer body
            let bodyParts = [
                "photo_base64=\(imageBase64)",
                "label=\(label.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? label)",
                "user_id=\(userID)",
                "confidence=\(claudeConfidence)"
            ]
            
            request.httpBody = bodyParts.joined(separator: "&").data(using: .utf8)
            
            // Envoyer requête
            let (data, response) = try await URLSession.shared.data(for: request)
            
            // Vérifier réponse
            if let httpResponse = response as? HTTPURLResponse {
                if httpResponse.statusCode == 200 {
                    // Succès!
                    if let result = try? JSONDecoder().decode(ContributionResponse.self, from: data) {
                        DispatchQueue.main.async {
                            if result.retraining_triggered {
                                self.contributionMessage = "🎉 \(result.message)"
                                // Vérifier modèle après réentraînement
                                Task {
                                    try? await Task.sleep(nanoseconds: 5_000_000_000) // 5s
                                    await self.checkForModelUpdate()
                                }
                            } else {
                                self.contributionMessage = "✅ \(result.message)"
                            }
                            self.isContributing = false
                        }
                        
                        print("📤 Photo contribuée: \(label) (\(result.total_for_label) total)")
                    }
                } else {
                    throw ContributionError.serverError("HTTP \(httpResponse.statusCode)")
                }
            }
            
        } catch {
            DispatchQueue.main.async {
                self.contributionMessage = "❌ Erreur: \(error.localizedDescription)"
                self.isContributing = false
            }
            print("❌ Erreur contribution: \(error)")
        }
    }
    
    
    // MARK: - 2. VÉRIFIER MISE À JOUR MODÈLE
    
    func checkForModelUpdate() async {
        """
        Vérifie si une nouvelle version du modèle est disponible
        """
        do {
            let url = backendURL.appendingPathComponent("/api/learning/latest_model")
            let (data, _) = try await URLSession.shared.data(from: url)
            
            if let response = try? JSONDecoder().decode(ModelResponse.self, from: data) {
                if response.status == "ready" {
                    DispatchQueue.main.async {
                        if response.version > self.modelVersion {
                            print("🆕 Nouvelle version modèle disponible: v\(response.version)")
                            self.modelUpdateAvailable = true
                            
                            // Télécharger et installer automatiquement
                            Task {
                                await self.downloadAndInstallModel(modelBase64: response.model_base64)
                            }
                        }
                    }
                }
            }
        } catch {
            // Silencieux - vérification en arrière-plan
        }
    }
    
    
    // MARK: - 3. TÉLÉCHARGER ET INSTALLER MODÈLE
    
    func downloadAndInstallModel(modelBase64: String) async {
        """
        Télécharge un nouveau modèle Core ML et l'installe
        """
        do {
            // Décoder base64
            guard let modelData = Data(base64Encoded: modelBase64) else {
                throw ContributionError.decodingError("Base64 invalide")
            }
            
            // Sauvegarder dans Documents
            let fileManager = FileManager.default
            let documentsPath = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let modelPath = documentsPath.appendingPathComponent("NutriScan.mlmodel")
            
            try modelData.write(to: modelPath)
            
            // Charger et compiler le modèle
            // NOTE: En production, utiliser MLModel.load() avec le nouveau modèle
            
            DispatchQueue.main.async {
                if let savedVersion = self.modelVersion as? Int {
                    self.modelVersion = savedVersion + 1
                }
                UserDefaults.standard.set(self.modelVersion, forKey: "modelVersion")
                
                print("✅ Modèle v\(self.modelVersion) installé!")
                self.modelUpdateAvailable = false
            }
            
        } catch {
            print("❌ Erreur installation modèle: \(error)")
        }
    }
    
    
    // MARK: - 4. CHARGER STATISTIQUES APPRENTISSAGE
    
    func loadLearningStats() async {
        """
        Récupère les statistiques du système d'apprentissage
        """
        do {
            let url = backendURL.appendingPathComponent("/api/learning/stats")
            let (data, _) = try await URLSession.shared.data(from: url)
            
            if let stats = try? JSONDecoder().decode(LearningStats.self, from: data) {
                DispatchQueue.main.async {
                    self.learningStats = stats
                }
            }
        } catch {
            print("⚠️  Erreur chargement stats: \(error)")
        }
    }
    
    
    // MARK: - 5. HISTORIQUE CONTRIBUTIONS UTILISATEUR
    
    func getUserContributionHistory() async -> [UserContribution]? {
        """
        Récupère l'historique des contributions de cet utilisateur
        """
        do {
            let url = backendURL.appendingPathComponent("/api/learning/user/\(userID)/history")
            let (data, _) = try await URLSession.shared.data(from: url)
            
            if let response = try? JSONDecoder().decode(UserHistoryResponse.self, from: data) {
                return response.contributions
            }
        } catch {
            print("⚠️  Erreur historique: \(error)")
        }
        
        return nil
    }
    
    
    // MARK: - 6. VÉRIFICATION AUTOMATIQUE
    
    private func startModelUpdateCheck() {
        """
        Vérifie la disponibilité de nouvelles versions toutes les minutes
        """
        updateCheckTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task {
                await self?.checkForModelUpdate()
            }
        }
    }
}


// MARK: - Modèles de réponse

struct ContributionResponse: Codable {
    let status: String
    let message: String
    let total_for_label: Int
    let user_id: String
    let retraining_triggered: Bool?
}

struct ModelResponse: Codable {
    let version: Int
    let status: String
    let model_base64: String
    let created_at: String?
    let training_photos: Int?
}

struct LearningStats: Codable {
    let total_contributions: Int
    let active_users: Int
    let labels: [LabelStat]
    let current_model_version: Int
    let model_status: String
    
    struct LabelStat: Codable {
        let label: String
        let count: Int
    }
}

struct UserHistoryResponse: Codable {
    let user_id: String
    let total: Int
    let contributions: [UserContribution]
}

struct UserContribution: Codable {
    let label: String
    let confidence: Float
    let timestamp: String
}


// MARK: - Erreurs

enum ContributionError: LocalizedError {
    case serverError(String)
    case decodingError(String)
    case networkError(URLError)
    
    var errorDescription: String? {
        switch self {
        case .serverError(let msg):
            return "Erreur serveur: \(msg)"
        case .decodingError(let msg):
            return "Erreur décodage: \(msg)"
        case .networkError(let error):
            return "Erreur réseau: \(error.localizedDescription)"
        }
    }
}
