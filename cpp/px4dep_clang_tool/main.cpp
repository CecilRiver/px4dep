// px4dep_clang_tool: a small, read-only Clang LibTooling indexer for PX4.
//
// It is intentionally scoped to the M1/M2 skeleton:
//   * functions, fields and parameter wrapper declarations in selected dirs
//   * ParamExt* -> Ekf::parameters field constructor bindings
//   * uORB Publication/Subscription declarations and publish/copy/update calls
//   * Commander Report / failsafe_flags / CHECK_FAILSAFE / mode capability uses
//   * a conservative call graph for in-scope functions
//
// Output is JSONL.  All records carry source locations and, when a declaration
// came from a macro, both spelling and expansion locations.

#include "clang/AST/ASTConsumer.h"
#include "clang/AST/ASTContext.h"
#include "clang/AST/Decl.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/Expr.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Index/USRGeneration.h"
#include "clang/Lex/Lexer.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"

#include <mutex>
#include <regex>
#include <set>
#include <string>
#include <vector>

using namespace clang;
using namespace clang::index;
using namespace clang::tooling;

namespace {

llvm::cl::OptionCategory ToolCategory("px4dep clang tool");
llvm::cl::opt<std::string> OutputPath("output", llvm::cl::desc("JSONL output path"),
                                      llvm::cl::Required, llvm::cl::cat(ToolCategory));
llvm::cl::opt<std::string> RepoRoot("repo-root", llvm::cl::desc("PX4 repository root"),
                                    llvm::cl::init("."), llvm::cl::cat(ToolCategory));
llvm::cl::list<std::string> Scopes("scope", llvm::cl::desc("repo-relative scope directory (repeatable)"),
                                   llvm::cl::ZeroOrMore, llvm::cl::cat(ToolCategory));
llvm::cl::opt<std::string> ToolVersion("tool-version", llvm::cl::desc("tool version string"),
                                       llvm::cl::init("0.1.0"), llvm::cl::cat(ToolCategory));

struct LocInfo {
  std::string expansion_file;
  unsigned expansion_line = 0;
  unsigned expansion_column = 0;
  unsigned expansion_offset = 0;

  std::string spelling_file;
  unsigned spelling_line = 0;
  unsigned spelling_column = 0;
  unsigned spelling_offset = 0;

  // The location consumers should normally use for display.
  std::string file;
  unsigned line = 0;
  unsigned column = 0;

  bool valid = false;
};

struct Config {
  std::string RepoRoot;
  std::vector<std::string> Scopes;
  std::string Version;
};

class JsonEmitter {
public:
  explicit JsonEmitter(llvm::raw_fd_ostream &Out) : Out(Out) {}

  void emit(llvm::json::Object Obj) {
    std::lock_guard<std::mutex> Lock(Mutex);
    llvm::json::OStream OS(Out);
    OS.value(llvm::json::Value(std::move(Obj)));
    Out << '\n';
  }

private:
  llvm::raw_fd_ostream &Out;
  std::mutex Mutex;
};

std::string repoRelative(StringRef Path, StringRef Root) {
  if (Path.empty()) {
    return {};
  }
  std::string RootStr = Root.str();
  if (!RootStr.empty() && RootStr.back() != '/') {
    RootStr += '/';
  }
  if (Path.startswith(RootStr)) {
    return Path.drop_front(RootStr.size()).str();
  }
  if (Path == Root) {
    return ".";
  }
  return Path.str();
}

bool inScope(StringRef File, const Config &Cfg) {
  if (File.empty()) {
    return false;
  }
  for (const std::string &Scope : Cfg.Scopes) {
    if (File == Scope) {
      return true;
    }
    if (File.startswith(Scope) && File.size() > Scope.size() && File[Scope.size()] == '/') {
      return true;
    }
  }
  return false;
}

void fillLoc(const SourceManager &SM, SourceLocation L, const Config &Cfg, std::string &File,
             unsigned &Line, unsigned &Column, unsigned &Offset) {
  if (L.isInvalid()) {
    return;
  }
  PresumedLoc PL = SM.getPresumedLoc(L);
  if (!PL.isValid()) {
    return;
  }
  File = repoRelative(PL.getFilename(), Cfg.RepoRoot);
  Line = PL.getLine();
  Column = PL.getColumn();
  Offset = SM.getFileOffset(L);
}

LocInfo locInfo(SourceLocation L, const SourceManager &SM, const Config &Cfg) {
  LocInfo Info;
  if (L.isInvalid()) {
    return Info;
  }
  SourceLocation Exp = SM.getExpansionLoc(L);
  SourceLocation Spell = SM.getSpellingLoc(L);
  fillLoc(SM, Exp, Cfg, Info.expansion_file, Info.expansion_line, Info.expansion_column,
          Info.expansion_offset);
  fillLoc(SM, Spell, Cfg, Info.spelling_file, Info.spelling_line, Info.spelling_column,
          Info.spelling_offset);

  Info.file = Info.expansion_file;
  Info.line = Info.expansion_line;
  Info.column = Info.expansion_column;
  Info.valid = !Info.file.empty();
  return Info;
}

void addLoc(llvm::json::Object &Obj, const LocInfo &Loc) {
  Obj["file"] = Loc.file;
  Obj["line"] = Loc.line;
  Obj["column"] = Loc.column;
  Obj["expansion_file"] = Loc.expansion_file;
  Obj["expansion_line"] = Loc.expansion_line;
  Obj["expansion_column"] = Loc.expansion_column;
  Obj["spelling_file"] = Loc.spelling_file;
  Obj["spelling_line"] = Loc.spelling_line;
  Obj["spelling_column"] = Loc.spelling_column;
}

std::string sourceText(SourceRange Range, const SourceManager &SM, const LangOptions &LangOpts) {
  if (Range.isInvalid()) {
    return {};
  }
  CharSourceRange CharRange = CharSourceRange::getTokenRange(Range);
  auto Text = Lexer::getSourceText(CharRange, SM, LangOpts);
  if (Text.empty()) {
    return {};
  }
  return Text.str();
}

std::string usrOf(const Decl *D) {
  if (!D) {
    return {};
  }
  llvm::SmallString<160> Buf;
  if (generateUSRForDecl(D, Buf)) {
    return {};
  }
  return std::string(Buf.str());
}

std::string qualifiedNameOf(const NamedDecl *D) {
  if (!D) {
    return {};
  }
  std::string Name = D->getQualifiedNameAsString();
  if (Name.empty()) {
    Name = D->getNameAsString();
  }
  return Name;
}

const Expr *stripExpr(const Expr *E) {
  while (E) {
    if (const auto *P = dyn_cast<ParenExpr>(E)) {
      E = P->getSubExpr();
      continue;
    }
    if (const auto *C = dyn_cast<ImplicitCastExpr>(E)) {
      E = C->getSubExpr();
      continue;
    }
    if (const auto *W = dyn_cast<ExprWithCleanups>(E)) {
      E = W->getSubExpr();
      continue;
    }
    if (const auto *M = dyn_cast<MaterializeTemporaryExpr>(E)) {
      E = M->getSubExpr();
      continue;
    }
    if (const auto *B = dyn_cast<CXXBindTemporaryExpr>(E)) {
      E = B->getSubExpr();
      continue;
    }
    if (const auto *C = dyn_cast<CXXConstructExpr>(E)) {
      if (C->getNumArgs() == 1) {
        E = C->getArg(0);
        continue;
      }
    }
    break;
  }
  return E;
}

struct RefInfo {
  std::string usr;
  std::string name;
  std::string type;
  bool resolved = false;
};

RefInfo refFromExpr(const Expr *E) {
  RefInfo Out;
  E = stripExpr(E);
  if (!E) {
    return Out;
  }
  if (const auto *ME = dyn_cast<MemberExpr>(E)) {
    if (const auto *VD = dyn_cast<ValueDecl>(ME->getMemberDecl())) {
      Out.usr = usrOf(VD);
      Out.name = VD->getNameAsString();
      Out.type = VD->getType().getAsString();
      Out.resolved = true;
    }
    return Out;
  }
  if (const auto *DRE = dyn_cast<DeclRefExpr>(E)) {
    if (const auto *VD = dyn_cast<ValueDecl>(DRE->getDecl())) {
      Out.usr = usrOf(VD);
      Out.name = VD->getNameAsString();
      Out.type = VD->getType().getAsString();
      Out.resolved = true;
    }
    return Out;
  }
  if (const auto *ASE = dyn_cast<ArraySubscriptExpr>(E)) {
    return refFromExpr(ASE->getBase());
  }
  if (const auto *OCE = dyn_cast<CXXOperatorCallExpr>(E)) {
    if (OCE->getNumArgs() > 0) {
      return refFromExpr(OCE->getArg(0));
    }
  }
  return Out;
}

struct TargetInfo {
  std::string usr;
  std::string name;
  std::string type;
  std::string expr;
  std::string index_expr;
  bool resolved = false;
};

TargetInfo targetFromExpr(const Expr *E, const SourceManager &SM, const LangOptions &LangOpts) {
  TargetInfo Out;
  E = stripExpr(E);
  if (!E) {
    return Out;
  }
  Out.expr = sourceText(E->getSourceRange(), SM, LangOpts);

  if (const auto *ME = dyn_cast<MemberExpr>(E)) {
    if (const auto *VD = dyn_cast<ValueDecl>(ME->getMemberDecl())) {
      Out.usr = usrOf(VD);
      Out.name = VD->getNameAsString();
      Out.type = VD->getType().getAsString();
      Out.resolved = true;
    }
    return Out;
  }
  if (const auto *DRE = dyn_cast<DeclRefExpr>(E)) {
    if (const auto *VD = dyn_cast<ValueDecl>(DRE->getDecl())) {
      Out.usr = usrOf(VD);
      Out.name = VD->getNameAsString();
      Out.type = VD->getType().getAsString();
      Out.resolved = true;
    }
    return Out;
  }
  if (const auto *ASE = dyn_cast<ArraySubscriptExpr>(E)) {
    TargetInfo Base = targetFromExpr(ASE->getBase(), SM, LangOpts);
    Base.index_expr = sourceText(ASE->getIdx()->getSourceRange(), SM, LangOpts);
    Base.expr = Out.expr;
    return Base;
  }
  if (const auto *OCE = dyn_cast<CXXOperatorCallExpr>(E)) {
    if (OCE->getOperator() == OO_Call && OCE->getNumArgs() >= 2) {
      TargetInfo Base = targetFromExpr(OCE->getArg(0), SM, LangOpts);
      Base.index_expr = sourceText(OCE->getArg(1)->getSourceRange(), SM, LangOpts);
      Base.expr = Out.expr;
      return Base;
    }
  }
  return Out;
}

bool isParamWrapperType(StringRef TypeStr, std::string &ParamName, std::string &WrapperKind) {
  size_t L = TypeStr.find('<');
  size_t R = TypeStr.rfind('>');
  if (L == StringRef::npos || R == StringRef::npos || R <= L) {
    return false;
  }
  StringRef Head = TypeStr.slice(0, L);
  // Param wrappers are always named Param* in PX4.
  size_t Colon = Head.rfind("::");
  StringRef Leaf = (Colon == StringRef::npos) ? Head : Head.substr(Colon + 2);
  if (!Leaf.startswith("Param")) {
    return false;
  }
  StringRef Inner = TypeStr.slice(L + 1, R).trim();
  if (!Inner.contains("params::") && !Inner.contains("px4::params")) {
    return false;
  }
  size_t InnerColon = Inner.rfind("::");
  StringRef Name = (InnerColon == StringRef::npos) ? Inner : Inner.substr(InnerColon + 2);
  ParamName = Name.trim().str();
  WrapperKind = Leaf.str();
  return !ParamName.empty();
}

void collectOrbIds(StringRef Text, std::vector<std::string> &Out) {
  static const std::regex OrbCallRe(R"(ORB_ID\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))");
  static const std::regex OrbScopeRe(R"(ORB_ID\s*::\s*([A-Za-z_][A-Za-z0-9_]*))");
  std::string S = Text.str();
  const std::regex *Patterns[] = {&OrbCallRe, &OrbScopeRe};
  for (const std::regex *Pattern : Patterns) {
    for (std::sregex_iterator It(S.begin(), S.end(), *Pattern), End; It != End; ++It) {
      std::string Name = (*It)[1].str();
      bool Seen = false;
      for (const std::string &Existing : Out) {
        if (Existing == Name) {
          Seen = true;
          break;
        }
      }
      if (!Seen) {
        Out.push_back(Name);
      }
    }
  }
}

llvm::json::Array stringArray(const std::vector<std::string> &Values) {
  llvm::json::Array Arr;
  for (const std::string &Value : Values) {
    Arr.push_back(Value);
  }
  return Arr;
}

bool endsWith(StringRef Value, StringRef Suffix) {
  return Value.size() >= Suffix.size() && Value.endswith(Suffix);
}

bool isFailsafeFlagsType(QualType QualTypeValue) {
  QualType Unwrapped = QualTypeValue.getNonReferenceType();
  const Type *Raw = Unwrapped.getTypePtrOrNull();
  while (Raw && Raw->isPointerType()) {
    Unwrapped = Raw->getPointeeType().getNonReferenceType();
    Raw = Unwrapped.getTypePtrOrNull();
  }
  if (!Raw) {
    return false;
  }
  const RecordDecl *Record = Unwrapped->getAsRecordDecl();
  return Record && Record->getName() == "failsafe_flags_s";
}

bool containsStmt(const Stmt *Root, const Stmt *Target) {
  if (!Root || !Target) {
    return false;
  }
  if (Root == Target) {
    return true;
  }
  for (const Stmt *Child : Root->children()) {
    if (containsStmt(Child, Target)) {
      return true;
    }
  }
  return false;
}

bool isActionEnum(const EnumDecl *Enum) {
  if (!Enum) {
    return false;
  }
  const std::string Qualified = Enum->getQualifiedNameAsString();
  return Qualified == "FailsafeBase::Action" ||
         (Enum->getName() == "Action" && endsWith(Qualified, "::Action"));
}

bool isNavModesEnum(const EnumDecl *Enum) {
  if (!Enum) {
    return false;
  }
  return Enum->getName() == "NavModes" ||
         endsWith(Enum->getQualifiedNameAsString(), "::NavModes");
}

void addUnique(std::vector<std::string> &Values, const std::string &Value) {
  if (Value.empty()) {
    return;
  }
  for (const std::string &Existing : Values) {
    if (Existing == Value) {
      return;
    }
  }
  Values.push_back(Value);
}

void collectExprFacts(const Stmt *S, std::vector<std::string> &Modes,
                      std::vector<std::string> &NavStates, std::vector<std::string> &Flags,
                      std::vector<std::string> &Actions) {
  if (!S) {
    return;
  }
  if (const auto *DRE = dyn_cast<DeclRefExpr>(S)) {
    if (const auto *ECD = dyn_cast<EnumConstantDecl>(DRE->getDecl())) {
      if (const EnumDecl *Enum = dyn_cast<EnumDecl>(ECD->getDeclContext())) {
        const std::string Name = ECD->getNameAsString();
        if (isNavModesEnum(Enum)) {
          addUnique(Modes, Name);
        } else if (isActionEnum(Enum)) {
          addUnique(Actions, Name);
        } else if (StringRef(Name).startswith("NAVIGATION_STATE_")) {
          addUnique(NavStates, Name);
        }
      }
    }
  }
  if (const auto *ME = dyn_cast<MemberExpr>(S)) {
    if (const auto *Field = dyn_cast<FieldDecl>(ME->getMemberDecl())) {
      if (isFailsafeFlagsType(ME->getBase()->getType())) {
        addUnique(Flags, Field->getNameAsString());
      }
    }
  }
  for (const Stmt *Child : S->children()) {
    collectExprFacts(Child, Modes, NavStates, Flags, Actions);
  }
}

std::string flagFromOffsetOf(const Stmt *S) {
  if (!S) {
    return {};
  }
  if (const auto *Offset = dyn_cast<OffsetOfExpr>(S)) {
    for (unsigned I = Offset->getNumComponents(); I > 0; --I) {
      const OffsetOfNode &Component = Offset->getComponent(I - 1);
      if (Component.getKind() == OffsetOfNode::Field && Component.getField()) {
        return Component.getField()->getNameAsString();
      }
      if (Component.getKind() == OffsetOfNode::Identifier && Component.getFieldName()) {
        return Component.getFieldName()->getName().str();
      }
    }
  }
  for (const Stmt *Child : S->children()) {
    std::string Name = flagFromOffsetOf(Child);
    if (!Name.empty()) {
      return Name;
    }
  }
  return {};
}

struct ExprRef {
  std::string qualified_name;
  std::string name;
  std::string parent_class;
  std::string receiver_expr;
};

bool isInterestingMemberParent(StringRef Parent) {
  if (Parent.empty()) {
    return false;
  }
  return Parent.startswith("estimator::") || Parent.startswith("Ekf") ||
         Parent.startswith("ekf") || Parent.endswith("_s") ||
         Parent.contains("parameters") || Parent.contains("control_status") ||
         Parent.contains("fault_status");
}

bool accessIsWrite(const MemberExpr *ME, const std::vector<const Stmt *> &Stack,
                   std::string &WriteOp) {
  for (auto It = Stack.rbegin(); It != Stack.rend(); ++It) {
    if (*It == ME) {
      continue;
    }
    if (const auto *BO = dyn_cast<BinaryOperator>(*It)) {
      if (BO->isAssignmentOp() && containsStmt(BO->getLHS(), ME)) {
        WriteOp = BO->getOpcodeStr().str();
        return true;
      }
    }
    if (const auto *UO = dyn_cast<UnaryOperator>(*It)) {
      if ((UO->isIncrementOp() || UO->isDecrementOp()) &&
          containsStmt(UO->getSubExpr(), ME)) {
        WriteOp = UO->isIncrementOp() ? "++" : "--";
        return true;
      }
    }
  }
  return false;
}

const BinaryOperator *findWriteAssign(const MemberExpr *ME,
                                      const std::vector<const Stmt *> &Stack) {
  for (auto It = Stack.rbegin(); It != Stack.rend(); ++It) {
    if (*It == ME) {
      continue;
    }
    if (const auto *BO = dyn_cast<BinaryOperator>(*It)) {
      if (BO->isAssignmentOp() && containsStmt(BO->getLHS(), ME)) {
        return BO;
      }
    }
  }
  return nullptr;
}

void collectExprRefs(const Stmt *S, const SourceManager &SM, const LangOptions &LangOpts,
                     std::vector<ExprRef> &Fields, std::vector<std::string> &Locals) {
  if (!S) {
    return;
  }
  if (const auto *ME = dyn_cast<MemberExpr>(S)) {
    if (const auto *Field = dyn_cast<FieldDecl>(ME->getMemberDecl())) {
      if (!Field->getName().empty()) {
        ExprRef Ref;
        Ref.qualified_name = qualifiedNameOf(Field);
        Ref.name = Field->getNameAsString();
        if (const auto *Parent = dyn_cast<CXXRecordDecl>(Field->getParent())) {
          Ref.parent_class = Parent->getQualifiedNameAsString();
        }
        Ref.receiver_expr = sourceText(ME->getBase()->getSourceRange(), SM, LangOpts);
        if (isInterestingMemberParent(Ref.parent_class)) {
          bool Seen = false;
          for (const ExprRef &Existing : Fields) {
            if (Existing.qualified_name == Ref.qualified_name &&
                Existing.receiver_expr == Ref.receiver_expr) {
              Seen = true;
              break;
            }
          }
          if (!Seen) {
            Fields.push_back(std::move(Ref));
          }
        }
      }
    }
    // Do not also record the receiver chain as a separate field read: the
    // outermost member expression already carries the full field identity.
    if (!isa<MemberExpr>(ME->getBase())) {
      collectExprRefs(ME->getBase(), SM, LangOpts, Fields, Locals);
    }
    for (const Stmt *Child : ME->children()) {
      if (Child == ME->getBase()) {
        continue;
      }
      collectExprRefs(Child, SM, LangOpts, Fields, Locals);
    }
    return;
  }
  if (const auto *DRE = dyn_cast<DeclRefExpr>(S)) {
    if (const auto *VD = dyn_cast<VarDecl>(DRE->getDecl())) {
      if (VD->isLocalVarDecl() || isa<ParmVarDecl>(VD)) {
        addUnique(Locals, VD->getNameAsString());
      }
    }
    return;
  }
  for (const Stmt *Child : S->children()) {
    collectExprRefs(Child, SM, LangOpts, Fields, Locals);
  }
}

bool isUorbType(StringRef TypeStr, bool &IsPublication, std::string &UorbClass,
                std::string &MessageType) {
  size_t Pos = TypeStr.find("uORB::");
  if (Pos == StringRef::npos) {
    return false;
  }
  StringRef Tail = TypeStr.substr(Pos + 6);
  size_t End = Tail.find_first_of("< {");
  StringRef Class = (End == StringRef::npos) ? Tail : Tail.substr(0, End);
  if (Class.startswith("Publication")) {
    IsPublication = true;
  } else if (Class.startswith("Subscription")) {
    IsPublication = false;
  } else {
    return false;
  }
  UorbClass = Class.str();
  size_t L = Tail.find('<');
  size_t R = Tail.rfind('>');
  if (L != StringRef::npos && R != StringRef::npos && R > L) {
    MessageType = Tail.slice(L + 1, R).trim().str();
  }
  return true;
}

class Px4DepVisitor : public RecursiveASTVisitor<Px4DepVisitor> {
public:
  Px4DepVisitor(ASTContext &Ctx, JsonEmitter &Emitter, const Config &Cfg, std::string TU)
      : SM(Ctx.getSourceManager()), LangOpts(Ctx.getLangOpts()), Emitter(Emitter), Cfg(Cfg),
        TU(std::move(TU)) {}

  bool TraverseDecl(Decl *D) {
    FunctionDecl *Prev = CurrentFn;
    if (auto *FD = dyn_cast_or_null<FunctionDecl>(D)) {
      CurrentFn = FD;
    }
    bool Result = RecursiveASTVisitor::TraverseDecl(D);
    CurrentFn = Prev;
    return Result;
  }

  bool TraverseStmt(Stmt *S) {
    if (!S) {
      return true;
    }
    StmtStack.push_back(S);
    bool Result = RecursiveASTVisitor::TraverseStmt(S);
    StmtStack.pop_back();
    return Result;
  }

  bool VisitFunctionDecl(FunctionDecl *FD) {
    if (FD->isImplicit() || !FD->isThisDeclarationADefinition() || !FD->hasBody()) {
      return true;
    }
    LocInfo Loc = locInfo(FD->getLocation(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }
    llvm::json::Object Obj;
    Obj["record"] = "entity";
    Obj["kind"] = "function";
    Obj["usr"] = usrOf(FD);
    Obj["qualified_name"] = qualifiedNameOf(FD);
    Obj["type"] = FD->getType().getAsString();
    Obj["is_method"] = isa<CXXMethodDecl>(FD);
    Obj["is_constructor"] = isa<CXXConstructorDecl>(FD);
    Obj["is_static"] = FD->isStatic();
    if (const auto *MD = dyn_cast<CXXMethodDecl>(FD)) {
      if (const auto *Parent = MD->getParent()) {
        Obj["parent_class"] = Parent->getQualifiedNameAsString();
      }
    }
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool VisitFieldDecl(FieldDecl *FD) {
    if (FD->isImplicit() || FD->getName().empty()) {
      return true;
    }
    LocInfo Loc = locInfo(FD->getLocation(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }

    const std::string TypeStr = FD->getType().getAsString();
    std::string ParamName;
    std::string WrapperKind;
    const bool IsWrapper = isParamWrapperType(TypeStr, ParamName, WrapperKind);

    if (IsWrapper) {
      llvm::json::Object Obj;
      Obj["record"] = "param_wrapper";
      Obj["usr"] = usrOf(FD);
      Obj["field_name"] = FD->getNameAsString();
      Obj["qualified_name"] = qualifiedNameOf(FD);
      Obj["parameter_name"] = ParamName;
      Obj["wrapper_kind"] = WrapperKind;
      Obj["type"] = TypeStr;
      if (const auto *Parent = dyn_cast<CXXRecordDecl>(FD->getParent())) {
        Obj["parent_class"] = Parent->getQualifiedNameAsString();
      }
      addLoc(Obj, Loc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
      return true;
    }

    bool IsPublication = false;
    std::string UorbClass;
    std::string MessageType;
    if (isUorbType(TypeStr, IsPublication, UorbClass, MessageType)) {
      std::string DeclText = sourceText(FD->getSourceRange(), SM, LangOpts);
      std::vector<std::string> Topics;
      collectOrbIds(DeclText, Topics);

      llvm::json::Object Obj;
      Obj["record"] = "uorb_decl";
      Obj["usr"] = usrOf(FD);
      Obj["variable"] = FD->getNameAsString();
      Obj["qualified_name"] = qualifiedNameOf(FD);
      Obj["role"] = IsPublication ? "publication" : "subscription";
      Obj["uorb_class"] = UorbClass;
      Obj["message_type"] = MessageType;
      Obj["instance_mode"] = UorbClass.find("Multi") != std::string::npos
                                 ? "multi"
                                 : (Topics.size() > 1 ? "selected" : "single");
      Obj["type"] = TypeStr;
      Obj["decl_text"] = DeclText;
      llvm::json::Array TopicArray;
      for (const std::string &T : Topics) {
        TopicArray.push_back(T);
      }
      Obj["topics"] = std::move(TopicArray);
      if (const auto *Parent = dyn_cast<CXXRecordDecl>(FD->getParent())) {
        Obj["parent_class"] = Parent->getQualifiedNameAsString();
      }
      addLoc(Obj, Loc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
      return true;
    }

    llvm::json::Object Obj;
    Obj["record"] = "entity";
    Obj["kind"] = "field";
    Obj["usr"] = usrOf(FD);
    Obj["qualified_name"] = qualifiedNameOf(FD);
    Obj["type"] = TypeStr;
    Obj["is_mutable"] = FD->isMutable();
    if (const auto *Parent = dyn_cast<CXXRecordDecl>(FD->getParent())) {
      Obj["parent_class"] = Parent->getQualifiedNameAsString();
      Obj["parent_kind"] = Parent->isStruct() ? "struct" : "class";
    }
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool VisitVarDecl(VarDecl *VD) {
    if (VD->isImplicit() || VD->getName().empty()) {
      return true;
    }
    const std::string TypeStr = VD->getType().getAsString();
    bool IsPublication = false;
    std::string UorbClass;
    std::string MessageType;
    if (!isUorbType(TypeStr, IsPublication, UorbClass, MessageType)) {
      return true;
    }
    LocInfo Loc = locInfo(VD->getLocation(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }

    std::string DeclText = sourceText(VD->getSourceRange(), SM, LangOpts);
    std::vector<std::string> Topics;
    collectOrbIds(DeclText, Topics);

    llvm::json::Object Obj;
    Obj["record"] = "uorb_decl";
    Obj["usr"] = usrOf(VD);
    Obj["variable"] = VD->getNameAsString();
    Obj["qualified_name"] = qualifiedNameOf(VD);
    Obj["role"] = IsPublication ? "publication" : "subscription";
    Obj["uorb_class"] = UorbClass;
    Obj["message_type"] = MessageType;
    Obj["instance_mode"] = UorbClass.find("Multi") != std::string::npos
                               ? "multi"
                               : (Topics.size() > 1 ? "selected" : "single");
    Obj["type"] = TypeStr;
    Obj["decl_text"] = DeclText;
    llvm::json::Array TopicArray;
    for (const std::string &T : Topics) {
      TopicArray.push_back(T);
    }
    Obj["topics"] = std::move(TopicArray);
    if (const auto *Parent = dyn_cast<CXXRecordDecl>(VD->getDeclContext())) {
      Obj["parent_class"] = Parent->getQualifiedNameAsString();
    }
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool VisitEnumConstantDecl(EnumConstantDecl *ECD) {
    const EnumDecl *Enum = dyn_cast<EnumDecl>(ECD->getDeclContext());
    if (!isActionEnum(Enum)) {
      return true;
    }
    LocInfo Loc = locInfo(ECD->getLocation(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }
    llvm::json::Object Obj;
    Obj["record"] = "enum_value";
    Obj["enum_name"] = Enum->getQualifiedNameAsString();
    Obj["value_name"] = ECD->getNameAsString();
    Obj["qualified_name"] = qualifiedNameOf(ECD);
    Obj["value"] = static_cast<int64_t>(ECD->getInitVal().getSExtValue());
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  void refsToJson(const std::vector<ExprRef> &Refs, llvm::json::Array &Out) {
    for (const ExprRef &Ref : Refs) {
      llvm::json::Object Obj;
      Obj["qualified_name"] = Ref.qualified_name;
      Obj["name"] = Ref.name;
      Obj["parent_class"] = Ref.parent_class;
      Obj["receiver_expr"] = Ref.receiver_expr;
      Out.push_back(std::move(Obj));
    }
  }

  void localsToJson(const std::vector<std::string> &Locals, llvm::json::Array &Out) {
    for (const std::string &Name : Locals) {
      Out.push_back(Name);
    }
  }

  bool isEkf2Loc(const LocInfo &Loc) const {
    return Loc.valid && Loc.file.rfind("src/modules/ekf2/", 0) == 0;
  }

  // Conditions of enclosing branches control statements in their body. A
  // condition that merely contains the target is a data dependency, not a
  // control dependency, so it is deliberately skipped here.
  void appendControlConditions(const Stmt *Target, llvm::json::Array &Out) {
    for (const Stmt *Parent : StmtStack) {
      if (Parent == Target) {
        continue;
      }
      const Expr *Cond = nullptr;
      const char *Kind = nullptr;

      if (const auto *If = dyn_cast<IfStmt>(Parent)) {
        if (If->getCond() && containsStmt(If->getCond(), Target)) {
          continue;
        }
        if ((If->getThen() && containsStmt(If->getThen(), Target)) ||
            (If->getElse() && containsStmt(If->getElse(), Target))) {
          Cond = If->getCond();
          Kind = "if";
        }

      } else if (const auto *Switch = dyn_cast<SwitchStmt>(Parent)) {
        if (Switch->getCond() && containsStmt(Switch->getCond(), Target)) {
          continue;
        }
        if (Switch->getBody() && containsStmt(Switch->getBody(), Target)) {
          Cond = Switch->getCond();
          Kind = "switch";
        }

      } else if (const auto *CO = dyn_cast<ConditionalOperator>(Parent)) {
        if (CO->getCond() && containsStmt(CO->getCond(), Target)) {
          continue;
        }
        if ((CO->getTrueExpr() && containsStmt(CO->getTrueExpr(), Target)) ||
            (CO->getFalseExpr() && containsStmt(CO->getFalseExpr(), Target))) {
          Cond = CO->getCond();
          Kind = "ternary";
        }

      } else if (const auto *While = dyn_cast<WhileStmt>(Parent)) {
        if (While->getCond() && containsStmt(While->getCond(), Target)) {
          continue;
        }
        if (While->getBody() && containsStmt(While->getBody(), Target)) {
          Cond = While->getCond();
          Kind = "while";
        }

      } else if (const auto *Do = dyn_cast<DoStmt>(Parent)) {
        if (Do->getCond() && containsStmt(Do->getCond(), Target)) {
          continue;
        }
        if (Do->getBody() && containsStmt(Do->getBody(), Target)) {
          Cond = Do->getCond();
          Kind = "do";
        }

      } else if (const auto *For = dyn_cast<ForStmt>(Parent)) {
        if (For->getCond() && containsStmt(For->getCond(), Target)) {
          continue;
        }
        if (For->getBody() && containsStmt(For->getBody(), Target)) {
          Cond = For->getCond();
          Kind = "for";
        }

      } else if (const auto *Range = dyn_cast<CXXForRangeStmt>(Parent)) {
        if (Range->getRangeInit() && containsStmt(Range->getRangeInit(), Target)) {
          continue;
        }
        if (Range->getBody() && containsStmt(Range->getBody(), Target)) {
          Cond = Range->getRangeInit();
          Kind = "range_for";
        }
      }

      if (!Cond) {
        continue;
      }

      std::vector<ExprRef> Fields;
      std::vector<std::string> Locals;
      collectExprRefs(Cond, SM, LangOpts, Fields, Locals);

      llvm::json::Object Obj;
      Obj["kind"] = Kind;
      Obj["text"] = sourceText(Cond->getSourceRange(), SM, LangOpts);
      if (const auto *BO = dyn_cast<BinaryOperator>(Cond->IgnoreParenImpCasts())) {
        Obj["operator"] = BO->getOpcodeStr().str();
      }
      LocInfo CondLoc = locInfo(Cond->getExprLoc(), SM, Cfg);
      Obj["line"] = CondLoc.line;
      Obj["column"] = CondLoc.column;
      llvm::json::Array FieldArray;
      refsToJson(Fields, FieldArray);
      Obj["members"] = std::move(FieldArray);
      llvm::json::Array LocalArray;
      localsToJson(Locals, LocalArray);
      Obj["locals"] = std::move(LocalArray);
      Out.push_back(std::move(Obj));
    }
  }

  std::pair<std::string, std::string> receiverIdentity(const Expr *Base) {
    const Expr *E = Base ? Base->IgnoreParenImpCasts() : nullptr;
    while (const auto *ME = dyn_cast_or_null<MemberExpr>(E)) {
      E = ME->getBase()->IgnoreParenImpCasts();
    }

    if (const auto *DRE = dyn_cast_or_null<DeclRefExpr>(E)) {
      if (const auto *FD = dyn_cast<FieldDecl>(DRE->getDecl())) {
        return {"member_field", qualifiedNameOf(FD)};
      }
      if (const auto *PVD = dyn_cast<ParmVarDecl>(DRE->getDecl())) {
        return {"parameter", PVD->getNameAsString()};
      }
      if (const auto *VD = dyn_cast<VarDecl>(DRE->getDecl())) {
        return {"local", VD->getNameAsString()};
      }
    }
    if (isa_and_nonnull<CXXThisExpr>(E)) {
      return {"this", "this"};
    }
    return {"expr", E ? sourceText(E->getSourceRange(), SM, LangOpts) : ""};
  }

  bool isBaseOfEnclosingMember(const MemberExpr *ME) const {
    for (const Stmt *Ancestor : StmtStack) {
      if (Ancestor == ME) {
        continue;
      }
      if (const auto *Parent = dyn_cast<MemberExpr>(Ancestor)) {
        if (Parent->getBase()->IgnoreParenImpCasts() == ME) {
          return true;
        }
      }
    }
    return false;
  }

  void emitMemberAccess(MemberExpr *ME, FieldDecl *Field) {
    LocInfo Loc = locInfo(ME->getMemberLoc(), SM, Cfg);
    if (!isEkf2Loc(Loc) || isBaseOfEnclosingMember(ME)) {
      return;
    }

    std::string WriteOp;
    const bool IsWrite = accessIsWrite(ME, StmtStack, WriteOp);
    std::vector<ExprRef> RhsFields;
    std::vector<std::string> RhsLocals;
    std::string RhsText;
    if (const BinaryOperator *BO = findWriteAssign(ME, StmtStack)) {
      RhsText = sourceText(BO->getRHS()->getSourceRange(), SM, LangOpts);
      collectExprRefs(BO->getRHS(), SM, LangOpts, RhsFields, RhsLocals);
      if (BO->getOpcode() != BO_Assign) {
        collectExprRefs(BO->getLHS(), SM, LangOpts, RhsFields, RhsLocals);
      }
    }

    auto [ReceiverKind, ReceiverRoot] = receiverIdentity(ME->getBase());

    llvm::json::Object Obj;
    Obj["record"] = "member_access";
    Obj["qualified_name"] = qualifiedNameOf(Field);
    Obj["field"] = Field->getNameAsString();
    Obj["parent_class"] = [&]() {
      std::string Parent;
      if (const auto *Decl = dyn_cast<CXXRecordDecl>(Field->getParent())) {
        Parent = Decl->getQualifiedNameAsString();
      }
      return Parent;
    }();
    Obj["type"] = Field->getType().getAsString();
    Obj["receiver_expr"] = sourceText(ME->getBase()->getSourceRange(), SM, LangOpts);
    Obj["receiver_kind"] = ReceiverKind;
    Obj["receiver_root"] = ReceiverRoot;
    Obj["access"] = IsWrite ? "write" : "read";
    Obj["write_op"] = WriteOp;
    Obj["rhs_text"] = RhsText;
    llvm::json::Array RhsMemberArray;
    refsToJson(RhsFields, RhsMemberArray);
    Obj["rhs_members"] = std::move(RhsMemberArray);
    llvm::json::Array RhsLocalArray;
    localsToJson(RhsLocals, RhsLocalArray);
    Obj["rhs_locals"] = std::move(RhsLocalArray);
    llvm::json::Array ConditionArray;
    appendControlConditions(ME, ConditionArray);
    Obj["control_conditions"] = std::move(ConditionArray);
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
  }

  void emitLocalWrite(const std::string &Name, const std::string &Type, const Expr *Init,
                      const Stmt *Site, const char *WriteKind, bool IsParameter) {
    LocInfo Loc = locInfo(Site->getBeginLoc(), SM, Cfg);
    if (!isEkf2Loc(Loc) || Name.empty()) {
      return;
    }
    std::vector<ExprRef> RhsFields;
    std::vector<std::string> RhsLocals;
    std::string RhsText;
    if (Init) {
      RhsText = sourceText(Init->getSourceRange(), SM, LangOpts);
      collectExprRefs(Init, SM, LangOpts, RhsFields, RhsLocals);
    }

    llvm::json::Object Obj;
    Obj["record"] = "local_write";
    Obj["variable"] = Name;
    Obj["type"] = Type;
    Obj["is_parameter"] = IsParameter;
    Obj["write_kind"] = WriteKind;
    Obj["rhs_text"] = RhsText;
    llvm::json::Array RhsMemberArray;
    refsToJson(RhsFields, RhsMemberArray);
    Obj["rhs_members"] = std::move(RhsMemberArray);
    llvm::json::Array RhsLocalArray;
    localsToJson(RhsLocals, RhsLocalArray);
    Obj["rhs_locals"] = std::move(RhsLocalArray);
    llvm::json::Array ConditionArray;
    appendControlConditions(Site, ConditionArray);
    Obj["control_conditions"] = std::move(ConditionArray);
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
  }

  void emitCallResult(CallExpr *CE, const Expr *Lhs, const Stmt *Site, const char *TargetKind,
                      const std::string &TargetName, bool IsParameter) {
    LocInfo Loc = locInfo(CE->getBeginLoc(), SM, Cfg);
    if (!isEkf2Loc(Loc)) {
      return;
    }
    FunctionDecl *Callee = CE->getDirectCallee();
    if (!Callee || Callee->isImplicit()) {
      return;
    }
    const std::string CalleeUsr = usrOf(Callee);
    if (CalleeUsr.empty()) {
      return;
    }

    llvm::json::Object Obj;
    Obj["record"] = "call_result";
    Obj["callee_usr"] = CalleeUsr;
    Obj["callee"] = qualifiedNameOf(Callee);
    Obj["target_kind"] = TargetKind;
    Obj["target_name"] = TargetName;
    Obj["is_parameter"] = IsParameter;
    if (const auto *ME = dyn_cast_or_null<MemberExpr>(Lhs ? Lhs->IgnoreParenImpCasts() : nullptr)) {
      if (const auto *Field = dyn_cast<FieldDecl>(ME->getMemberDecl())) {
        Obj["target_field"] = qualifiedNameOf(Field);
        auto [Kind, Root] = receiverIdentity(ME->getBase());
        Obj["target_receiver_kind"] = Kind;
        Obj["target_receiver_root"] = Root;
      }
    }
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    llvm::json::Array ConditionArray;
    appendControlConditions(Site, ConditionArray);
    Obj["control_conditions"] = std::move(ConditionArray);
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
  }

  void emitCallArgs(CallExpr *CE) {
    LocInfo Loc = locInfo(CE->getBeginLoc(), SM, Cfg);
    if (!isEkf2Loc(Loc)) {
      return;
    }
    FunctionDecl *Callee = CE->getDirectCallee();
    if (!Callee || Callee->isImplicit()) {
      return;
    }
    const std::string CalleeUsr = usrOf(Callee);
    if (CalleeUsr.empty()) {
      return;
    }

    for (unsigned I = 0; I < CE->getNumArgs(); ++I) {
      const Expr *Arg = CE->getArg(I);
      std::vector<ExprRef> Fields;
      std::vector<std::string> Locals;
      collectExprRefs(Arg, SM, LangOpts, Fields, Locals);
      if (Fields.empty() && Locals.empty()) {
        continue;
      }
      llvm::json::Object Obj;
      Obj["record"] = "call_arg";
      Obj["callee_usr"] = CalleeUsr;
      Obj["callee"] = qualifiedNameOf(Callee);
      Obj["arg_index"] = I;
      if (I < Callee->getNumParams()) {
        Obj["param_name"] = Callee->getParamDecl(I)->getNameAsString();
        Obj["param_type"] = Callee->getParamDecl(I)->getType().getAsString();
      }
      Obj["arg_text"] = sourceText(Arg->getSourceRange(), SM, LangOpts);
      llvm::json::Array FieldArray;
      refsToJson(Fields, FieldArray);
      Obj["members"] = std::move(FieldArray);
      llvm::json::Array LocalArray;
      localsToJson(Locals, LocalArray);
      Obj["locals"] = std::move(LocalArray);
      Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
      Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
      llvm::json::Array ConditionArray;
      appendControlConditions(CE, ConditionArray);
      Obj["control_conditions"] = std::move(ConditionArray);
      addLoc(Obj, Loc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
    }
  }

  void emitReturnRef(ReturnStmt *RS) {
    if (!CurrentFn || !RS->getRetValue()) {
      return;
    }
    LocInfo Loc = locInfo(RS->getBeginLoc(), SM, Cfg);
    if (!isEkf2Loc(Loc)) {
      return;
    }
    std::vector<ExprRef> Fields;
    std::vector<std::string> Locals;
    collectExprRefs(RS->getRetValue(), SM, LangOpts, Fields, Locals);
    if (Fields.empty() && Locals.empty()) {
      return;
    }
    llvm::json::Object Obj;
    Obj["record"] = "return_ref";
    Obj["caller_usr"] = usrOf(CurrentFn);
    Obj["caller"] = qualifiedNameOf(CurrentFn);
    Obj["text"] = sourceText(RS->getRetValue()->getSourceRange(), SM, LangOpts);
    llvm::json::Array FieldArray;
    refsToJson(Fields, FieldArray);
    Obj["members"] = std::move(FieldArray);
    llvm::json::Array LocalArray;
    localsToJson(Locals, LocalArray);
    Obj["locals"] = std::move(LocalArray);
    llvm::json::Array ConditionArray;
    appendControlConditions(RS, ConditionArray);
    Obj["control_conditions"] = std::move(ConditionArray);
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
  }

  bool VisitDeclStmt(DeclStmt *DS) {
    if (!CurrentFn) {
      return true;
    }
    for (Decl *D : DS->decls()) {
      const auto *VD = dyn_cast<VarDecl>(D);
      if (!VD || VD->isImplicit() || !VD->hasInit()) {
        continue;
      }
      emitLocalWrite(VD->getNameAsString(), VD->getType().getAsString(), VD->getInit(), DS,
                     "decl", false);
      if (const auto *Call = dyn_cast<CallExpr>(VD->getInit()->IgnoreParenImpCasts())) {
        emitCallResult(const_cast<CallExpr *>(Call), nullptr, DS, "local", VD->getNameAsString(),
                       false);
      }
    }
    return true;
  }

  bool VisitReturnStmt(ReturnStmt *RS) {
    emitReturnRef(RS);
    return true;
  }

  // Commander/Failsafe use of a parameter wrapper is not part of the EKF2
  // field-slice records.  Emit every wrapper read with its enclosing control
  // conditions so the Commander slice can distinguish a direct use in a
  // health/failsafe sink from a weaker function-level use.
  void emitParameterUse(MemberExpr *ME, FieldDecl *Field) {
    const std::string TypeStr = Field->getType().getCanonicalType().getAsString();
    if (TypeStr.find("do_not_explicitly_use_this_namespace::Param") ==
        std::string::npos) {
      return;
    }
    LocInfo Loc = locInfo(ME->getMemberLoc(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg) || isEkf2Loc(Loc)) {
      return;
    }

    std::string ParameterName;
    const std::string Marker = "px4::params::";
    size_t Pos = TypeStr.find(Marker);
    if (Pos != std::string::npos) {
      Pos += Marker.size();
      size_t End = Pos;
      while (End < TypeStr.size()) {
        const char C = TypeStr[End];
        if (!((C >= 'A' && C <= 'Z') || (C >= 'a' && C <= 'z') ||
              (C >= '0' && C <= '9') || C == '_')) {
          break;
        }
        ++End;
      }
      ParameterName = TypeStr.substr(Pos, End - Pos);
    }

    std::string UseKind = "reference";
    for (auto It = StmtStack.rbegin(); It != StmtStack.rend(); ++It) {
      if (const auto *MCE = dyn_cast<CXXMemberCallExpr>(*It)) {
        if (MCE->getImplicitObjectArgument() &&
            containsStmt(MCE->getImplicitObjectArgument(), ME)) {
          if (const FunctionDecl *UseCallee = MCE->getDirectCallee()) {
            UseKind = UseCallee->getNameAsString();
          }
          break;
        }
      }
    }

    llvm::json::Object Obj;
    Obj["record"] = "parameter_use";
    Obj["use_kind"] = UseKind;
    Obj["field_name"] = Field->getNameAsString();
    Obj["qualified_name"] = qualifiedNameOf(Field);
    Obj["parameter_name"] = ParameterName;
    Obj["type"] = TypeStr;
    Obj["receiver_expr"] = sourceText(ME->getBase()->getSourceRange(), SM, LangOpts);
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    llvm::json::Array ConditionArray;
    appendControlConditions(ME, ConditionArray);
    Obj["control_conditions"] = std::move(ConditionArray);
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
  }

  bool VisitMemberExpr(MemberExpr *ME) {
    const auto *Field = dyn_cast<FieldDecl>(ME->getMemberDecl());
    if (!Field || Field->getName().empty()) {
      return true;
    }
    emitParameterUse(ME, const_cast<FieldDecl *>(Field));
    emitMemberAccess(ME, const_cast<FieldDecl *>(Field));
    if (!isFailsafeFlagsType(ME->getBase()->getType())) {
      return true;
    }
    LocInfo Loc = locInfo(ME->getMemberLoc(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }

    bool IsWrite = false;
    std::string WriteOp;
    for (auto It = StmtStack.rbegin(); It != StmtStack.rend(); ++It) {
      if (*It == ME) {
        continue;
      }
      if (const auto *BO = dyn_cast<BinaryOperator>(*It)) {
        if (BO->isAssignmentOp() && containsStmt(BO->getLHS(), ME)) {
          IsWrite = true;
          WriteOp = BO->getOpcodeStr().str();
          break;
        }
      }
      if (const auto *UO = dyn_cast<UnaryOperator>(*It)) {
        if ((UO->isIncrementOp() || UO->isDecrementOp()) &&
            containsStmt(UO->getSubExpr(), ME)) {
          IsWrite = true;
          WriteOp = UO->isIncrementOp() ? "++" : "--";
          break;
        }
      }
    }

    std::string RhsText;
    if (IsWrite) {
      if (const BinaryOperator *BO = findWriteAssign(ME, StmtStack)) {
        RhsText = sourceText(BO->getRHS()->getSourceRange(), SM, LangOpts);
      }
    }

    llvm::json::Object Obj;
    Obj["record"] = "flag_access";
    Obj["field"] = Field->getNameAsString();
    Obj["qualified_name"] = "failsafe_flags_s::" + Field->getNameAsString();
    Obj["access"] = IsWrite ? "write" : "read";
    Obj["write_op"] = WriteOp;
    Obj["rhs_text"] = RhsText;
    Obj["receiver_expr"] = sourceText(ME->getBase()->getSourceRange(), SM, LangOpts);
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    llvm::json::Array FlagConditions;
    appendControlConditions(ME, FlagConditions);
    Obj["control_conditions"] = std::move(FlagConditions);
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool VisitDeclRefExpr(DeclRefExpr *DRE) {
    const auto *ECD = dyn_cast<EnumConstantDecl>(DRE->getDecl());
    if (!ECD) {
      return true;
    }
    const EnumDecl *Enum = dyn_cast<EnumDecl>(ECD->getDeclContext());
    if (!isActionEnum(Enum)) {
      return true;
    }
    LocInfo Loc = locInfo(DRE->getBeginLoc(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }

    bool InReturn = false;
    bool InAssignment = false;
    bool InComparison = false;
    for (auto It = StmtStack.rbegin(); It != StmtStack.rend(); ++It) {
      if (*It == DRE) {
        continue;
      }
      if (const auto *Return = dyn_cast<ReturnStmt>(*It)) {
        if (Return->getRetValue() && containsStmt(Return->getRetValue(), DRE)) {
          InReturn = true;
          break;
        }
      }
      if (const auto *BO = dyn_cast<BinaryOperator>(*It)) {
        if (BO->isAssignmentOp() && containsStmt(BO->getRHS(), DRE)) {
          InAssignment = true;
          break;
        }
        if (BO->isComparisonOp()) {
          InComparison = true;
          break;
        }
      }
    }

    llvm::json::Object Obj;
    Obj["record"] = "action_ref";
    Obj["action"] = ECD->getNameAsString();
    Obj["qualified_name"] = qualifiedNameOf(ECD);
    Obj["in_return"] = InReturn;
    Obj["context"] = InReturn ? "return"
                              : (InAssignment ? "assignment" : (InComparison ? "comparison" : "other"));
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    llvm::json::Array ActionConditions;
    appendControlConditions(DRE, ActionConditions);
    Obj["control_conditions"] = std::move(ActionConditions);
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool VisitBinaryOperator(BinaryOperator *BO) {
    if (!BO->isAssignmentOp()) {
      return true;
    }

    if (CurrentFn) {
      const Expr *Lhs = BO->getLHS()->IgnoreParenImpCasts();
      if (const auto *DRE = dyn_cast<DeclRefExpr>(Lhs)) {
        if (const auto *VD = dyn_cast<VarDecl>(DRE->getDecl())) {
          if (VD->isLocalVarDecl() || isa<ParmVarDecl>(VD)) {
            emitLocalWrite(VD->getNameAsString(), VD->getType().getAsString(), BO->getRHS(), BO,
                           BO->getOpcodeStr().str().c_str(), isa<ParmVarDecl>(VD));
            if (const auto *Call = dyn_cast<CallExpr>(BO->getRHS()->IgnoreParenImpCasts())) {
              emitCallResult(const_cast<CallExpr *>(Call), Lhs, BO, "local",
                             VD->getNameAsString(), isa<ParmVarDecl>(VD));
            }
          }
        }
      }
    }

    std::string RhsText = sourceText(BO->getRHS()->getSourceRange(), SM, LangOpts);
    std::vector<std::string> Topics;
    collectOrbIds(RhsText, Topics);
    if (Topics.empty()) {
      return true;
    }
    RefInfo Receiver = refFromExpr(BO->getLHS());
    if (!Receiver.resolved || Receiver.usr.empty()) {
      return true;
    }
    std::string RhsType = BO->getRHS()->getType().getAsString();
    bool IsPublication = false;
    std::string UorbClass;
    std::string MessageType;
    if (!isUorbType(RhsType, IsPublication, UorbClass, MessageType) &&
        !isUorbType(Receiver.type, IsPublication, UorbClass, MessageType)) {
      return true;
    }
    LocInfo Loc = locInfo(BO->getBeginLoc(), SM, Cfg);
    if (!Loc.valid || !inScope(Loc.file, Cfg)) {
      return true;
    }
    llvm::json::Object Obj;
    Obj["record"] = "uorb_init";
    Obj["receiver_usr"] = Receiver.usr;
    Obj["receiver"] = Receiver.name;
    Obj["role"] = IsPublication ? "publication" : "subscription";
    Obj["uorb_class"] = UorbClass;
    Obj["message_type"] = MessageType;
    Obj["topic_expr"] = RhsText;
    llvm::json::Array TopicArray;
    for (const std::string &T : Topics) {
      TopicArray.push_back(T);
    }
    Obj["topics"] = std::move(TopicArray);
    Obj["assignment"] = true;
    addLoc(Obj, Loc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

  bool TraverseConstructorInitializer(CXXCtorInitializer *Init) {
    if (Init->isWritten() && Init->isMemberInitializer()) {
      FieldDecl *Member = Init->getMember();
      if (Member) {
        const std::string MemberType = Member->getType().getAsString();
        std::string ParamName;
        std::string WrapperKind;
        if (isParamWrapperType(MemberType, ParamName, WrapperKind)) {
          TargetInfo Target = targetFromExpr(Init->getInit(), SM, LangOpts);
          LocInfo Loc = locInfo(Init->getSourceLocation(), SM, Cfg);
          llvm::json::Object Obj;
          Obj["record"] = "binding";
          Obj["src_usr"] = usrOf(Member);
          Obj["src_name"] = Member->getNameAsString();
          Obj["parameter_name"] = ParamName;
          Obj["dst_usr"] = Target.usr;
          Obj["dst_name"] = Target.name;
          Obj["dst_type"] = Target.type;
          Obj["target_expr"] = Target.expr;
          Obj["index_expr"] = Target.index_expr;
          Obj["resolved"] = Target.resolved;
          Obj["relation"] = "binds_to";
          addLoc(Obj, Loc);
          Obj["tu"] = TU;
          Emitter.emit(std::move(Obj));
        }

        bool IsPublication = false;
        std::string UorbClass;
        std::string MessageType;
        if (isUorbType(MemberType, IsPublication, UorbClass, MessageType)) {
          std::string InitText = sourceText(Init->getInit()->getSourceRange(), SM, LangOpts);
          std::vector<std::string> Topics;
          collectOrbIds(InitText, Topics);
          if (!Topics.empty()) {
            LocInfo Loc = locInfo(Init->getSourceLocation(), SM, Cfg);
            llvm::json::Object Obj;
            Obj["record"] = "uorb_init";
            Obj["receiver_usr"] = usrOf(Member);
            Obj["receiver"] = Member->getNameAsString();
            Obj["role"] = IsPublication ? "publication" : "subscription";
            Obj["uorb_class"] = UorbClass;
            Obj["message_type"] = MessageType;
            Obj["topic_expr"] = InitText;
            llvm::json::Array TopicArray;
            for (const std::string &T : Topics) {
              TopicArray.push_back(T);
            }
            Obj["topics"] = std::move(TopicArray);
            addLoc(Obj, Loc);
            Obj["tu"] = TU;
            Emitter.emit(std::move(Obj));
          }
        }
      }
    }
    return RecursiveASTVisitor::TraverseConstructorInitializer(Init);
  }

  bool VisitCallExpr(CallExpr *CE) {
    FunctionDecl *Callee = CE->getDirectCallee();
    if (!Callee) {
      return true;
    }
    emitCallArgs(CE);
    LocInfo CallLoc = locInfo(CE->getBeginLoc(), SM, Cfg);
    if (!CallLoc.valid || !inScope(CallLoc.file, Cfg)) {
      return true;
    }

    const std::string Method = Callee->getNameAsString();
    const std::string CalleeName = qualifiedNameOf(Callee);
    const bool UorbOp = (Method == "publish" || Method == "copy" || Method == "update" ||
                         Method == "ChangeInstance" || Method == "change_instance");
    if (UorbOp) {
      if (const auto *MCE = dyn_cast<CXXMemberCallExpr>(CE)) {
        RefInfo Receiver = refFromExpr(MCE->getImplicitObjectArgument());
        bool IsPublication = false;
        std::string UorbClass;
        std::string MessageType;
        // `update()` also exists on parameter wrappers; only emit a uORB edge
        // when the receiver really is a uORB publication/subscription.
        if (isUorbType(Receiver.type, IsPublication, UorbClass, MessageType)) {
          llvm::json::Object Obj;
          Obj["record"] = "uorb_call";
          Obj["operation"] = Method;
          Obj["receiver_usr"] = Receiver.usr;
          Obj["receiver"] = Receiver.name;
          Obj["receiver_type"] = Receiver.type;
          Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
          Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
          Obj["argument"] = CE->getNumArgs() > 0
                                ? sourceText(CE->getArg(0)->getSourceRange(), SM, LangOpts)
                                : "";
          addLoc(Obj, CallLoc);
          Obj["tu"] = TU;
          Emitter.emit(std::move(Obj));
          return true;
        }
      }
    }

    emitHealthRecords(CE, Callee, Method, CallLoc);

    const bool ParamApi = (Method == "param_find" || Method == "param_get" ||
                           Method == "param_set" || Method == "param_find_no_notification");
    const bool FrameworkApi =
        (Method == "armingCheckFailure" || Method == "setIsPresent" ||
         Method == "clearArmingBits" || Method == "clearCanRunBits" ||
         Method == "failsafeFlags" || Method == "modeCanRun" ||
         Method == "checkModeFallback");
    LocInfo CalleeLoc = locInfo(Callee->getLocation(), SM, Cfg);
    const bool CalleeInScope = CalleeLoc.valid && inScope(CalleeLoc.file, Cfg);
    if (!ParamApi && !FrameworkApi && !CalleeInScope) {
      return true;
    }

    llvm::json::Object Obj;
    Obj["record"] = "call";
    Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
    Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
    Obj["callee_usr"] = usrOf(Callee);
    Obj["callee"] = CalleeName;
    Obj["method"] = Method;
    Obj["api"] = ParamApi ? "parameter" : (FrameworkApi ? "health_framework" : "local");
    Obj["callee_file"] = CalleeLoc.file;
    Obj["callee_line"] = CalleeLoc.line;
    Obj["num_args"] = CE->getNumArgs();
    llvm::json::Array Args;
    for (unsigned I = 0; I < CE->getNumArgs() && I < 4; ++I) {
      llvm::json::Object A;
      A["index"] = I;
      A["text"] = sourceText(CE->getArg(I)->getSourceRange(), SM, LangOpts);
      Args.push_back(std::move(A));
    }
    Obj["args"] = std::move(Args);
    addLoc(Obj, CallLoc);
    Obj["tu"] = TU;
    Emitter.emit(std::move(Obj));
    return true;
  }

private:
  std::string argText(const CallExpr *CE, unsigned Index) const {
    if (Index >= CE->getNumArgs()) {
      return {};
    }
    return sourceText(CE->getArg(Index)->getSourceRange(), SM, LangOpts);
  }

  std::string macroName(const CallExpr *CE) const {
    SourceLocation Loc = CE->getBeginLoc();
    StringRef Name = Lexer::getImmediateMacroName(Loc, SM, LangOpts);
    if (Name.empty()) {
      Name = Lexer::getImmediateMacroName(SM.getExpansionLoc(Loc), SM, LangOpts);
    }
    return Name.str();
  }

  std::string receiverType(const CallExpr *CE, const FunctionDecl *Callee) const {
    if (const auto *MCE = dyn_cast<CXXMemberCallExpr>(CE)) {
      return MCE->getObjectType().getAsString();
    }
    if (const auto *Method = dyn_cast<CXXMethodDecl>(Callee)) {
      if (const CXXRecordDecl *Parent = Method->getParent()) {
        return Parent->getQualifiedNameAsString();
      }
    }
    return {};
  }

  std::string receiverExpr(const CallExpr *CE) const {
    if (const auto *MCE = dyn_cast<CXXMemberCallExpr>(CE)) {
      if (const Expr *Object = MCE->getImplicitObjectArgument()) {
        return sourceText(Object->getSourceRange(), SM, LangOpts);
      }
    }
    return {};
  }

  bool isNegated(const CallExpr *CE) const {
    for (const Stmt *Parent : StmtStack) {
      if (const auto *Unary = dyn_cast<UnaryOperator>(Parent)) {
        if (Unary->getOpcode() == UO_LNot && containsStmt(Unary->getSubExpr(), CE)) {
          return true;
        }
      }
    }
    return false;
  }

  llvm::json::Array callArguments(const CallExpr *CE, unsigned Limit) const {
    llvm::json::Array Args;
    for (unsigned I = 0; I < CE->getNumArgs() && I < Limit; ++I) {
      llvm::json::Object Arg;
      Arg["index"] = I;
      Arg["text"] = argText(CE, I);
      Args.push_back(std::move(Arg));
    }
    return Args;
  }

  void emitHealthRecords(CallExpr *CE, FunctionDecl *Callee, const std::string &Method,
                         const LocInfo &CallLoc) {
    static const std::set<std::string> HealthMethods = {
        "healthFailure",      "armingCheckFailure", "setIsPresent",
        "setHealth",          "clearArmingBits",    "clearCanRunBits",
        "canArm",             "canRun",             "modePreventsArming",
        "failsafeFlags",      "addExternalEvent"};
    static const std::set<std::string> ModeMethods = {
        "modeCanRun", "canRun", "canArm", "modePreventsArming"};

    const std::string ReceiverType = receiverType(CE, Callee);
    const std::string ReceiverExpr = receiverExpr(CE);

    if (HealthMethods.count(Method)) {
      llvm::json::Object Obj;
      Obj["record"] = "health_call";
      Obj["method"] = Method;
      Obj["receiver_type"] = ReceiverType;
      Obj["receiver_expr"] = ReceiverExpr;
      Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
      Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
      Obj["num_args"] = CE->getNumArgs();
      Obj["args"] = callArguments(CE, 8);
      llvm::json::Array HealthConditions;
      appendControlConditions(CE, HealthConditions);
      Obj["control_conditions"] = std::move(HealthConditions);

      int ModesIndex = -1;
      if (Method == "healthFailure" || Method == "armingCheckFailure" ||
          Method == "clearArmingBits" || Method == "clearCanRunBits") {
        ModesIndex = 0;
      } else if (Method == "addExternalEvent") {
        ModesIndex = 1;
      }

      std::vector<std::string> Modes;
      std::vector<std::string> NavStates;
      std::vector<std::string> Flags;
      std::vector<std::string> Actions;
      if (ModesIndex >= 0 && static_cast<unsigned>(ModesIndex) < CE->getNumArgs()) {
        collectExprFacts(CE->getArg(ModesIndex), Modes, NavStates, Flags, Actions);
      }
      Obj["mode_groups"] = stringArray(Modes);
      Obj["nav_states"] = stringArray(NavStates);
      Obj["mode_flags"] = stringArray(Flags);
      Obj["mode_dynamic"] = ModesIndex >= 0 && Modes.empty() && NavStates.empty() &&
                             Flags.empty() && argText(CE, ModesIndex).size() > 0;
      addLoc(Obj, CallLoc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
    }

    if (ModeMethods.count(Method)) {
      const int ModeIndex = Method == "modeCanRun" ? 1 : 0;
      std::vector<std::string> Modes;
      std::vector<std::string> NavStates;
      std::vector<std::string> Flags;
      std::vector<std::string> Actions;
      std::string ModeExpr;
      if (static_cast<unsigned>(ModeIndex) < CE->getNumArgs()) {
        const Expr *ModeArg = CE->getArg(ModeIndex);
        ModeExpr = sourceText(ModeArg->getSourceRange(), SM, LangOpts);
        collectExprFacts(ModeArg, Modes, NavStates, Flags, Actions);
      }
      llvm::json::Object Obj;
      Obj["record"] = "mode_check";
      Obj["method"] = Method;
      Obj["mode_expr"] = ModeExpr;
      Obj["mode_groups"] = stringArray(Modes);
      Obj["nav_states"] = stringArray(NavStates);
      Obj["mode_flags"] = stringArray(Flags);
      Obj["mode_dynamic"] = Modes.empty() && NavStates.empty() && Flags.empty() &&
                             !ModeExpr.empty();
      Obj["negated"] = isNegated(CE);
      Obj["receiver_type"] = ReceiverType;
      Obj["receiver_expr"] = ReceiverExpr;
      Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
      Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
      Obj["args"] = callArguments(CE, 4);
      llvm::json::Array ModeConditions;
      appendControlConditions(CE, ModeConditions);
      Obj["control_conditions"] = std::move(ModeConditions);
      addLoc(Obj, CallLoc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
    }

    if (Method == "checkFailsafe") {
      std::vector<std::string> Modes;
      std::vector<std::string> NavStates;
      std::vector<std::string> Flags;
      std::vector<std::string> Actions;
      std::string Flag;
      std::string CurrentCondition;
      std::string LastCondition;
      std::string Options;
      if (CE->getNumArgs() > 0) {
        Flag = flagFromOffsetOf(CE->getArg(0));
      }
      if (CE->getNumArgs() > 1) {
        LastCondition = argText(CE, 1);
      }
      if (CE->getNumArgs() > 2) {
        CurrentCondition = argText(CE, 2);
        collectExprFacts(CE->getArg(2), Modes, NavStates, Flags, Actions);
      }
      if (CE->getNumArgs() > 0) {
        Options = argText(CE, CE->getNumArgs() - 1);
        collectExprFacts(CE->getArg(CE->getNumArgs() - 1), Modes, NavStates, Flags, Actions);
      }
      std::vector<std::string> OptionActions;
      if (CE->getNumArgs() > 0) {
        std::vector<std::string> IgnoredModes;
        std::vector<std::string> IgnoredStates;
        std::vector<std::string> IgnoredFlags;
        collectExprFacts(CE->getArg(CE->getNumArgs() - 1), IgnoredModes, IgnoredStates,
                         IgnoredFlags, OptionActions);
      }
      llvm::json::Object Obj;
      Obj["record"] = "failsafe_check";
      Obj["macro"] = macroName(CE) == "CHECK_FAILSAFE" ? "CHECK_FAILSAFE" : "";
      Obj["flag"] = Flag;
      Obj["condition_flags"] = stringArray(Flags);
      Obj["actions"] = stringArray(OptionActions);
      Obj["action_dynamic"] = OptionActions.empty();
      Obj["options"] = Options;
      Obj["last_condition"] = LastCondition;
      Obj["current_condition"] = CurrentCondition;
      Obj["receiver_type"] = ReceiverType;
      Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
      Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
      Obj["args"] = callArguments(CE, 8);
      llvm::json::Array FailsafeConditions;
      appendControlConditions(CE, FailsafeConditions);
      Obj["control_conditions"] = std::move(FailsafeConditions);
      addLoc(Obj, CallLoc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
    }

    if (Method == "checkModeFallback") {
      std::vector<std::string> Modes;
      std::vector<std::string> NavStates;
      std::vector<std::string> Flags;
      std::vector<std::string> Actions;
      std::string ModeExpr;
      if (CE->getNumArgs() > 1) {
        ModeExpr = argText(CE, 1);
        collectExprFacts(CE->getArg(1), Modes, NavStates, Flags, Actions);
      }
      llvm::json::Object Obj;
      Obj["record"] = "fallback_check";
      Obj["mode_expr"] = ModeExpr;
      Obj["nav_states"] = stringArray(NavStates);
      Obj["mode_flags"] = stringArray(Flags);
      Obj["mode_dynamic"] = NavStates.empty() && Flags.empty() && !ModeExpr.empty();
      Obj["receiver_type"] = ReceiverType;
      Obj["caller_usr"] = CurrentFn ? usrOf(CurrentFn) : "";
      Obj["caller"] = CurrentFn ? qualifiedNameOf(CurrentFn) : "";
      Obj["args"] = callArguments(CE, 4);
      llvm::json::Array FallbackConditions;
      appendControlConditions(CE, FallbackConditions);
      Obj["control_conditions"] = std::move(FallbackConditions);
      addLoc(Obj, CallLoc);
      Obj["tu"] = TU;
      Emitter.emit(std::move(Obj));
    }
  }

  SourceManager &SM;
  const LangOptions &LangOpts;
  JsonEmitter &Emitter;
  const Config &Cfg;
  std::string TU;
  FunctionDecl *CurrentFn = nullptr;
  std::vector<const Stmt *> StmtStack;
};

class Px4DepConsumer : public ASTConsumer {
public:
  Px4DepConsumer(ASTContext &Ctx, JsonEmitter &Emitter, const Config &Cfg, std::string TU)
      : Ctx(Ctx), Emitter(Emitter), Cfg(Cfg), TU(std::move(TU)) {}

  void HandleTranslationUnit(ASTContext &Ctx) override {
    Px4DepVisitor Visitor(Ctx, Emitter, Cfg, TU);
    Visitor.TraverseDecl(Ctx.getTranslationUnitDecl());
  }

private:
  ASTContext &Ctx;
  JsonEmitter &Emitter;
  const Config &Cfg;
  std::string TU;
};

class Px4DepAction : public ASTFrontendAction {
public:
  Px4DepAction(JsonEmitter &Emitter, const Config &Cfg) : Emitter(Emitter), Cfg(Cfg) {}

  bool BeginSourceFileAction(CompilerInstance &CI) override {
    SourceManager &SM = CI.getSourceManager();
    TU = repoRelative(SM.getFilename(SM.getLocForStartOfFile(SM.getMainFileID())), Cfg.RepoRoot);
    return true;
  }

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI, StringRef) override {
    return std::make_unique<Px4DepConsumer>(CI.getASTContext(), Emitter, Cfg, TU);
  }

private:
  JsonEmitter &Emitter;
  Config Cfg;
  std::string TU;
};

class Px4DepActionFactory : public FrontendActionFactory {
public:
  Px4DepActionFactory(JsonEmitter &Emitter, const Config &Cfg) : Emitter(Emitter), Cfg(Cfg) {}
  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<Px4DepAction>(Emitter, Cfg);
  }

private:
  JsonEmitter &Emitter;
  Config Cfg;
};

CommandLineArguments filterArguments(const CommandLineArguments &Args, StringRef) {
  CommandLineArguments Out;
  for (size_t I = 0; I < Args.size(); ++I) {
    StringRef A = Args[I];
    if (A == "-o") {
      ++I;
      continue;
    }
    if (A == "-MT" || A == "-MF") {
      ++I;
      continue;
    }
    if (A == "-MD" || A == "-MMD" || A == "-c") {
      continue;
    }
    if (A.startswith("-Werror") || A == "-Wfatal-errors" || A == "-Wlogical-op" ||
        A == "-Wno-stringop-truncation" || A == "-Wunused-but-set-variable" ||
        A == "-Wno-unused-but-set-variable" || A == "-fcheck-new" ||
        A == "-fno-strength-reduce" || A == "-fdiagnostics-color=always") {
      continue;
    }
    Out.push_back(A.str());
  }
  Out.push_back("-Wno-unknown-warning-option");
  Out.push_back("-fno-color-diagnostics");
  return Out;
}

} // namespace

int main(int argc, const char **argv) {
  auto ExpectedParser = CommonOptionsParser::create(argc, argv, ToolCategory, llvm::cl::OneOrMore);
  if (!ExpectedParser) {
    llvm::errs() << llvm::toString(ExpectedParser.takeError()) << "\n";
    return 1;
  }
  CommonOptionsParser &Parser = ExpectedParser.get();

  Config Cfg;
  Cfg.RepoRoot = RepoRoot;
  Cfg.Scopes = Scopes;
  Cfg.Version = ToolVersion;

  std::error_code EC;
  llvm::raw_fd_ostream Out(OutputPath, EC, llvm::sys::fs::OF_Text);
  if (EC) {
    llvm::errs() << "cannot open output: " << EC.message() << "\n";
    return 1;
  }
  JsonEmitter Emitter(Out);

  ClangTool Tool(Parser.getCompilations(), Parser.getSourcePathList());
  Tool.appendArgumentsAdjuster(filterArguments);

  Px4DepActionFactory Factory(Emitter, Cfg);
  int Result = Tool.run(&Factory);
  Out.flush();
  return Result;
}
