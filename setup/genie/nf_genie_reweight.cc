// Universe reweighting for neutrino-factory over GENIE Reweight's engines.
//
// GENIE Reweight ships grwght1p (one dial, 1D scan) and grwghtnp (correlated
// throws, but only a subset of engines, and NormCCRES wired to the CCQE engine);
// neither evaluates a set of independent multi-dial universes. This driver does:
// for every row of the universes file it sets all dials, reconfigures once, and
// weights every event. Universes are the outer loop because Reconfigure()
// rebuilds the engines' model copies; each event is re-read for every universe
// because the engines modify the interaction while computing a weight.
//
// Usage: nf_genie_reweight <events.ghep.root> <universes.txt> <weights.root>
//                          --tune <tune> [--message-thresholds <files>]
//   universes.txt: line 1 holds one "<GSyst name>:<kind>" per column, kind
//     "scale" or "raw"; each further line is one universe, one value per column.
//     scale: the value multiplies the tune default (1 = nominal). GENIE's own 1
//            sigma table is overridden to 1 for these dials, so GENIE's dial
//            (p = p_def * (1 + dial * err)) is exactly value - 1.
//     raw:   the value is passed to GENIE unchanged (the [0,1] switch dials).
//   weights.root: TTree "weights", branch "weights[N]/D", one entry per input
//     event in input order: sigma(universe) / sigma(tune), NaN left as is.
//
// Built inside $GENIE_REWEIGHT/src/Apps by setup/Dockerfile.genie and
// setup/apptainer/genie.def, with grwght1p's link line.

#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include <TFile.h>
#include <TTree.h>

#include "Framework/EventGen/EventRecord.h"
#include "Framework/Messenger/Messenger.h"
#include "Framework/Ntuple/NtpMCEventRecord.h"
#include "Framework/Utils/AppInit.h"
#include "Framework/Utils/RunOpt.h"

#include "RwCalculators/GReWeightINuke.h"
#include "RwCalculators/GReWeightNonResonanceBkg.h"
#include "RwCalculators/GReWeightNuXSecCCQE.h"
#include "RwCalculators/GReWeightNuXSecCCRES.h"
#include "RwCalculators/GReWeightNuXSecCOH.h"
#include "RwCalculators/GReWeightNuXSecDIS.h"
#include "RwCalculators/GReWeightResonanceDecay.h"
#include "RwCalculators/GReWeightXSecMEC.h"
#include "RwFramework/GReWeight.h"
#include "RwFramework/GSyst.h"
#include "RwFramework/GSystUncertainty.h"

using namespace genie;
using namespace genie::rew;

static void fail(const std::string &msg) {
  std::cerr << "[nf_genie_reweight] ERROR: " << msg << std::endl;
  std::exit(2);
}

// The engine that handles each dial. Engines are adopted only when a dial needs
// them: GReWeightINuke exit()s in its constructor unless the tune's FSI model is
// hA2018. The dials are exactly the allowlist in src/neutrino_factory/universes.py
// (GENIE_SCALE_DIALS and GENIE_SWITCH_DIALS), each checked on a real sample.
static GReWeightI *make_engine(const std::string &key) {
  if (key == "xsec_ccqe") return new GReWeightNuXSecCCQE;
  if (key == "xsec_ccres") return new GReWeightNuXSecCCRES;
  if (key == "xsec_nonresbkg") return new GReWeightNonResonanceBkg;
  if (key == "xsec_dis") return new GReWeightNuXSecDIS;
  if (key == "xsec_coh") return new GReWeightNuXSecCOH;
  if (key == "xsec_mec") return new GReWeightXSecMEC;
  if (key == "hadro_intranuke") return new GReWeightINuke;
  if (key == "hadro_res_decay") return new GReWeightResonanceDecay;
  return nullptr;
}

static std::string engine_for(const std::string &name) {
  static const std::map<std::string, std::string> table = {
      {"MaCCQE", "xsec_ccqe"},
      {"NormCCQE", "xsec_ccqe"},
      {"MaCCRES", "xsec_ccres"},
      {"MvCCRES", "xsec_ccres"},
      {"NormCCRES", "xsec_ccres"},
      {"NonRESBGvpCC1pi", "xsec_nonresbkg"},
      {"NonRESBGvnCC1pi", "xsec_nonresbkg"},
      {"NonRESBGvpCC2pi", "xsec_nonresbkg"},
      {"NonRESBGvnCC2pi", "xsec_nonresbkg"},
      {"AhtBY", "xsec_dis"},
      {"BhtBY", "xsec_dis"},
      {"CV1uBY", "xsec_dis"},
      {"CV2uBY", "xsec_dis"},
      {"MaCOHpi", "xsec_coh"},
      {"R0COHpi", "xsec_coh"},
      {"NormCCCOH", "xsec_coh"},
      {"NormCCMEC", "xsec_mec"},
      {"MFP_pi", "hadro_intranuke"},
      {"MFP_N", "hadro_intranuke"},
      {"FrCEx_pi", "hadro_intranuke"},
      {"FrInel_pi", "hadro_intranuke"},
      {"FrAbs_pi", "hadro_intranuke"},
      {"FrPiProd_pi", "hadro_intranuke"},
      {"FrCEx_N", "hadro_intranuke"},
      {"FrInel_N", "hadro_intranuke"},
      {"FrAbs_N", "hadro_intranuke"},
      {"FrPiProd_N", "hadro_intranuke"},
      {"RPA_CCQE", "xsec_ccqe"},
      {"XSecShape_CCMEC", "xsec_mec"},
      {"DecayAngMEC", "xsec_mec"},
      {"Theta_Delta2Npi", "hadro_res_decay"},
  };
  auto it = table.find(name);
  return it == table.end() ? "" : it->second;
}

int main(int argc, char **argv) {
  if (argc < 4) {
    fail("usage: nf_genie_reweight <events.ghep.root> <universes.txt> <weights.root> "
         "--tune <tune> [--message-thresholds <files>]");
  }
  RunOpt::Instance()->ReadFromCommandLine(argc, argv);
  utils::app_init::MesgThresholds(RunOpt::Instance()->MesgThresholdFiles());
  // Every engine logs each weight at NOTICE; per event and per universe.
  Messenger::Instance()->SetPriorityLevel("ReW", log4cpp::Priority::WARN);
  Messenger::Instance()->SetPriorityLevel("RwMEC", log4cpp::Priority::WARN);
  if (!RunOpt::Instance()->Tune()) fail("--tune is required (the tune the events were generated with)");
  RunOpt::Instance()->BuildTune();

  std::ifstream spec(argv[2]);
  if (!spec) fail(std::string("cannot open ") + argv[2]);
  std::string line;
  std::getline(spec, line);
  std::vector<GSyst_t> dials;
  std::vector<bool> scale;
  std::map<std::string, std::vector<GSyst_t>> engines;
  {
    std::istringstream header(line);
    std::string column;
    while (header >> column) {
      auto colon = column.find(':');
      if (colon == std::string::npos) fail("column '" + column + "' is not <name>:<kind>");
      std::string name = column.substr(0, colon), kind = column.substr(colon + 1);
      if (kind != "scale" && kind != "raw") fail("column '" + column + "': kind must be scale or raw");
      GSyst_t syst = GSyst::FromString(name);
      if (syst == kNullSystematic) fail("'" + name + "' is not a GENIE Reweight systematic");
      std::string key = engine_for(name);
      if (key.empty()) fail("'" + name + "' has no engine in nf_genie_reweight");
      dials.push_back(syst);
      scale.push_back(kind == "scale");
      engines[key].push_back(syst);
      if (kind == "scale") GSystUncertainty::Instance()->SetUncertainty(syst, 1.0, 1.0);
    }
  }
  if (dials.empty()) fail("no dials on the first line of the universes file");

  std::vector<std::vector<double>> universes;
  while (std::getline(spec, line)) {
    std::istringstream row(line);
    std::vector<double> values;
    double v;
    while (row >> v) values.push_back(v);
    if (values.empty()) continue;
    if (values.size() != dials.size()) fail("universe row with the wrong number of values");
    universes.push_back(values);
  }
  if (universes.empty()) fail("no universes in the universes file");

  GReWeight rw;
  for (auto &[key, systs] : engines) {
    rw.AdoptWghtCalc(key, make_engine(key));
    // Recompute the nominal dsigma with the engine's own default model instead
    // of reading event.DiffXSec(): for the Berger-Sehgal RES and COH models of
    // G18_10a_02_11a the stored value differs from the model's own at the
    // stored kinematics by an event-dependent factor (1x to 38x), which turns
    // every weight into noise (docs/generators/genie.md, "Upstream issues").
    rw.WghtCalc(key)->UseOldWeightFromFile(false);
    // Pick the engine mode the dials belong to. Defaults otherwise: CCQE's
    // follows the tune's axial form factor, RES's is NormAndMaMvShape.
    for (GSyst_t s : systs) {
      if (s == kXSecTwkDial_MaCCQE)
        dynamic_cast<GReWeightNuXSecCCQE *>(rw.WghtCalc(key))->SetMode(GReWeightNuXSecCCQE::kModeMa);
      if (s == kXSecTwkDial_MaCCRES || s == kXSecTwkDial_MvCCRES)
        dynamic_cast<GReWeightNuXSecCCRES *>(rw.WghtCalc(key))->SetMode(GReWeightNuXSecCCRES::kModeMaMv);
    }
    for (GSyst_t s : systs) {
      if (!rw.WghtCalc(key)->IsHandled(s)) {
        fail("'" + GSyst::AsString(s) + "' has no effect under tune " +
             RunOpt::Instance()->Tune()->Name() + " (engine " + key +
             " does not handle it in its current mode)");
      }
    }
  }

  TFile in(argv[1], "READ");
  TTree *tree = dynamic_cast<TTree *>(in.Get("gtree"));
  if (!tree) fail(std::string("no gtree in ") + argv[1]);
  NtpMCEventRecord *rec = nullptr;
  tree->SetBranchAddress("gmcrec", &rec);
  const Long64_t n_events = tree->GetEntries();
  const size_t n_univ = universes.size();
  std::vector<double> weights(n_events * n_univ);

  for (size_t u = 0; u < n_univ; ++u) {
    for (size_t j = 0; j < dials.size(); ++j) {
      double value = universes[u][j];
      rw.Systematics().Set(dials[j], scale[j] ? value - 1.0 : value);
    }
    rw.Reconfigure();
    for (Long64_t i = 0; i < n_events; ++i) {
      tree->GetEntry(i);
      EventRecord &event = *(rec->event);
      // Cross-section engines return event.Weight() * ratio; gevgen's are 1.
      if (event.Weight() != 1.0) fail("event with weight != 1; weighted GHEP files are not supported");
      weights[i * n_univ + u] = rw.CalcWeight(event);
      rec->Clear();
    }
  }
  in.Close();

  TFile out(argv[3], "RECREATE");
  TTree t("weights", "neutrino-factory universe weights");
  std::vector<double> row(n_univ);
  t.Branch("weights", row.data(), ("weights[" + std::to_string(n_univ) + "]/D").c_str());
  for (Long64_t i = 0; i < n_events; ++i) {
    for (size_t u = 0; u < n_univ; ++u) row[u] = weights[i * n_univ + u];
    t.Fill();
  }
  t.Write();
  out.Close();
  return 0;
}
