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
  // The struck initial-state hadronic system, summed over its nucleons, for the
  // true invariant hadronic mass. n_nuc = 0 marks "none found" -> normalizer
  // blanks; pdgnuc is the first nucleon's PDG and is informational only.
  Double_t nuc_e_gev = 0.0, nuc_px_gev = 0.0, nuc_py_gev = 0.0, nuc_pz_gev = 0.0;
  Int_t n_nuc = 0;
  Int_t pdgnuc = 0;
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
  out_tree->Branch("n_nuc", &n_nuc, "n_nuc/I");
  out_tree->Branch("pdgnuc", &pdgnuc, "pdgnuc/I");
  out_tree->Branch("nuc_e_gev", &nuc_e_gev, "nuc_e_gev/D");
  out_tree->Branch("nuc_px_gev", &nuc_px_gev, "nuc_px_gev/D");
  out_tree->Branch("nuc_py_gev", &nuc_py_gev, "nuc_py_gev/D");
  out_tree->Branch("nuc_pz_gev", &nuc_pz_gev, "nuc_pz_gev/D");

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
    //
    // The same scan collects the struck initial-state nucleons, which are exactly
    // the nucleons that precede that lepton: one normally, two for 2p2h. They are
    // summed rather than written out singly, because the invariant mass of a 2p2h
    // event is that of the correlated *pair* -- which is also what GENIE hands
    // over, as its gst struck-nucleon branches carry the two-nucleon cluster.
    // n_nuc is written alongside so a later change of that policy needs no
    // container rebuild. Nucleons after the lepton are final-state and must not
    // be counted, which is why the loop stops there.
    pdglep = 0;
    lep_e_gev = lep_px_gev = lep_py_gev = lep_pz_gev = 0.0;
    n_nuc = 0;
    pdgnuc = 0;
    nuc_e_gev = nuc_px_gev = nuc_py_gev = nuc_pz_gev = 0.0;
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
      if (abs_pid == 2112 || abs_pid == 2212) {
        if (n_nuc == 0) pdgnuc = part->fPID;
        ++n_nuc;
        nuc_e_gev += part->fP.E() / 1000.0;
        nuc_px_gev += part->fP.Px() / 1000.0;
        nuc_py_gev += part->fP.Py() / 1000.0;
        nuc_pz_gev += part->fP.Pz() / 1000.0;
      }
    }
    if (pdglep == 0) {
      // No lepton found, so the scan ran to the end of the array and anything it
      // collected may be final-state. The normalizer blanks the whole event on
      // pdglep == 0 anyway; drop the nucleons rather than leave a wrong sum.
      n_nuc = 0;
      pdgnuc = 0;
      nuc_e_gev = nuc_px_gev = nuc_py_gev = nuc_pz_gev = 0.0;
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
