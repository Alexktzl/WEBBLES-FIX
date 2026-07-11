pub struct Account { pub name: u32 }
fn main() {
    let v = Account { name: 1 };
    println!("{}", v.label);
}
