// Flatten NEUT's native event output into a uproot-readable tree.
//
// neutroot2 writes a "neuttree" whose "vectorbranch" holds NeutVect objects.
// uproot can deserialize NeutVect's scalar members from the file's streamers,
// but chokes on the nested TObjArray of NeutPart ("invalid class-tag
// reference"), so the native file cannot be read from Python at all. This
// macro is the NEUT counterpart of GENIE's gntpc: it runs inside the container,
// where libNEUTClass and the NEUT headers are available, and writes a tree of
// plain scalars plus the normalization histograms the normalizer needs.
//
// Run it through setup/neut/nf-neut-flatten, which supplies the .include path
// (without it Cling reports "unknown type name 'NeutVect'").

#include "neutpart.h"
#include "neutvect.h"

#include "TClass.h"
#include "TFile.h"
#include "TH1.h"
#include "TH1D.h"
#include "TKey.h"
#include "TSystem.h"
#include "TTree.h"

// Capacity of the fixed final-state buffers. NEUT events are far smaller than
// this even for DIS on a heavy target; overflowing it is treated as a fatal
// error rather than truncated, since a silently short particle list would
// understate the multiplicities the normalizer derives from it.
const Int_t kMaxFinalState = 200;

void nf_flatten(const char* in_path, const char* out_path) {
  gSystem->Load("libNEUTClass");

  TFile fin(in_path, "READ");
  if (fin.IsZombie()) {
    Error("nf_flatten", "cannot open NEUT output file %s", in_path);
    gSystem->Exit(1);
  }
  TTree* in_tree = (TTree*)fin.Get("neuttree");
  if (!in_tree) {
    Error("nf_flatten", "no 'neuttree' in %s", in_path);
    gSystem->Exit(1);
  }

  NeutVect* nv = new NeutVect();
  in_tree->SetBranchAddress("vectorbranch", &nv);

  TFile fout(out_path, "RECREATE");
  TTree* out_tree = new TTree("nf_neut", "flattened NEUT events");

  Int_t mode = 0;
  Int_t pdgnu = 0;
  Double_t enu_gev = 0.0;
  Double_t totcrs = 0.0;
  // Four-vectors for the derived kinematics (Q^2, x, y, lepton angle), written
  // in GeV so the flat tree is single-unit and the normalizer converts nothing.
  Double_t nu_px_gev = 0.0, nu_py_gev = 0.0, nu_pz_gev = 0.0;
  Double_t lep_e_gev = 0.0, lep_px_gev = 0.0, lep_py_gev = 0.0, lep_pz_gev = 0.0;
  Int_t pdglep = 0;  // 0 marks "no outgoing lepton found" -> normalizer blanks
  // The post-FSI final state, as variable-length branches. The normalizer
  // summarizes it into multiplicities and hadronic energy; it is written out
  // here because uproot cannot reach NeutVect's particle array at all.
  Int_t n_fs = 0;
  Int_t fs_pdg[kMaxFinalState];
  Double_t fs_e_gev[kMaxFinalState];
  Double_t fs_px_gev[kMaxFinalState];
  Double_t fs_py_gev[kMaxFinalState];
  Double_t fs_pz_gev[kMaxFinalState];
  out_tree->Branch("mode", &mode, "mode/I");
  out_tree->Branch("pdgnu", &pdgnu, "pdgnu/I");
  out_tree->Branch("enu_gev", &enu_gev, "enu_gev/D");
  out_tree->Branch("totcrs", &totcrs, "totcrs/D");
  out_tree->Branch("nu_px_gev", &nu_px_gev, "nu_px_gev/D");
  out_tree->Branch("nu_py_gev", &nu_py_gev, "nu_py_gev/D");
  out_tree->Branch("nu_pz_gev", &nu_pz_gev, "nu_pz_gev/D");
  out_tree->Branch("pdglep", &pdglep, "pdglep/I");
  out_tree->Branch("lep_e_gev", &lep_e_gev, "lep_e_gev/D");
  out_tree->Branch("lep_px_gev", &lep_px_gev, "lep_px_gev/D");
  out_tree->Branch("lep_py_gev", &lep_py_gev, "lep_py_gev/D");
  out_tree->Branch("lep_pz_gev", &lep_pz_gev, "lep_pz_gev/D");
  out_tree->Branch("n_fs", &n_fs, "n_fs/I");
  out_tree->Branch("fs_pdg", fs_pdg, "fs_pdg[n_fs]/I");
  out_tree->Branch("fs_e_gev", fs_e_gev, "fs_e_gev[n_fs]/D");
  out_tree->Branch("fs_px_gev", fs_px_gev, "fs_px_gev[n_fs]/D");
  out_tree->Branch("fs_py_gev", fs_py_gev, "fs_py_gev[n_fs]/D");
  out_tree->Branch("fs_pz_gev", fs_pz_gev, "fs_pz_gev[n_fs]/D");

  const Long64_t n_entries = in_tree->GetEntries();
  for (Long64_t i = 0; i < n_entries; ++i) {
    in_tree->GetEntry(i);
    mode = nv->Mode;
    totcrs = nv->Totcrs;
    // PartInfo(0) is the incoming neutrino; NeutPart four-momenta are in MeV.
    NeutPart* probe = nv->PartInfo(0);
    if (!probe) {
      Error("nf_flatten", "entry %lld has no incoming particle", i);
      gSystem->Exit(1);
    }
    pdgnu = probe->fPID;
    enu_gev = probe->fP.E() / 1000.0;
    nu_px_gev = probe->fP.Px() / 1000.0;
    nu_py_gev = probe->fP.Py() / 1000.0;
    nu_pz_gev = probe->fP.Pz() / 1000.0;

    // Find the outgoing lepton by PDG, NOT at a fixed index. The usual layout is
    // [0] = beam neutrino, [1] = struck nucleon, [2] = outgoing lepton, but 2p2h
    // (Mode 2) has *two* initial-state nucleons at [1] and [2], putting the
    // lepton at [3]; reading PartInfo(2) there yields a neutron and silently
    // corrupts the kinematics for the whole MEC channel. The first lepton at
    // index >= 1 is the primary outgoing lepton -- the charged lepton for CC,
    // the scattered neutrino for NC. Leptons do not rescatter, so there is no
    // FSI copy to confuse this.
    pdglep = 0;
    lep_e_gev = lep_px_gev = lep_py_gev = lep_pz_gev = 0.0;
    for (int j = 1; j < nv->Npart(); ++j) {
      NeutPart* part = nv->PartInfo(j);
      if (!part) continue;
      const int abs_pid = abs(part->fPID);
      if (abs_pid >= 11 && abs_pid <= 16) {
        pdglep = part->fPID;
        lep_e_gev = part->fP.E() / 1000.0;
        lep_px_gev = part->fP.Px() / 1000.0;
        lep_py_gev = part->fP.Py() / 1000.0;
        lep_pz_gev = part->fP.Pz() / 1000.0;
        break;
      }
    }

    // The post-FSI final state: the particles that actually leave the nucleus.
    // NeutVect's array also holds the initial-state nucleons (fStatus = -1) and
    // particles killed or replaced during FSI (fStatus != 0, e.g. 3 = absorbed,
    // 7 = produced child particles), so it is filtered on NEUT's own flags
    // rather than by index. The outgoing lepton is included here as well; the
    // normalizer excludes leptons when it summarizes the list.
    n_fs = 0;
    for (int j = 1; j < nv->Npart(); ++j) {
      NeutPart* part = nv->PartInfo(j);
      if (!part) continue;
      if (!part->fIsAlive || part->fStatus != 0) continue;
      if (n_fs >= kMaxFinalState) {
        Error("nf_flatten",
              "entry %lld has more than %d final-state particles; refusing to "
              "truncate the list, which would understate the multiplicities",
              i, kMaxFinalState);
        gSystem->Exit(1);
      }
      fs_pdg[n_fs] = part->fPID;
      fs_e_gev[n_fs] = part->fP.E() / 1000.0;
      fs_px_gev[n_fs] = part->fP.Px() / 1000.0;
      fs_py_gev[n_fs] = part->fP.Py() / 1000.0;
      fs_pz_gev[n_fs] = part->fP.Pz() / 1000.0;
      ++n_fs;
    }
    out_tree->Fill();
  }

  fout.cd();
  out_tree->Write();

  // neutroot2 stores the flux histogram it sampled and the corresponding event
  // rate (flux x sigma) alongside the events. Their integral ratio is the
  // flux-averaged total cross section in 1e-38 cm^2 per nucleon, which is the
  // only normalization information NEUT emits — carry it across.
  //
  // The names are flavour-dependent: neutroot2 formats them as "flux_%s" /
  // "evtrt_%s" with its own short flavour token ("numu", "numub", "nue",
  // "nueb"; verified in the NEUT 5.7.0 binary), alongside a generic
  // "fluxhisto" / "ratehisto" copy that is the only pair present for a beam it
  // has no token for. Rather than encode that mapping, copy every histogram
  // verbatim and let NeutNormalizer pick the pair out by prefix — that is also
  // what NUISANCE does (GetObjectWithName).
  TIter next(fin.GetListOfKeys());
  while (TKey* key = (TKey*)next()) {
    TClass* cls = TClass::GetClass(key->GetClassName());
    if (!cls || !cls->InheritsFrom(TH1::Class())) continue;
    TH1* hist = (TH1*)key->ReadObj();
    if (!hist) continue;
    fout.cd();
    hist->Write(key->GetName());
  }

  fout.Close();
  printf("nf_flatten: wrote %lld events to %s\n", n_entries, out_path);
}
