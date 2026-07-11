pub struct Customer { pub name: u32 }
fn main() {
    let v = Customer { name: 1 };
    println!("{}", v.label);
}
